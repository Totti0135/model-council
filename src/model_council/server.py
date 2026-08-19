#!/usr/bin/env python3
"""Model Council — an MCP server.

Puts other LLMs on the table as tools, so your assistant can consult them,
compare their answers, and synthesize a conclusion inside one conversation.

Any number of models, from any mix of OpenAI-compatible and Anthropic-compatible
endpoints. See config.py for where the roster comes from.

Two ways to run it. Over stdio (the default) an MCP client launches one copy per
user, and each user supplies their own keys. Over HTTP (`--http`) one deployment
holds one set of keys and serves a team, who configure a URL and no secret at
all; see access.py for what then decides who may reach it.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path
from typing import Literal

from mcp.server import MCPServer
from pydantic import BaseModel, Field
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from model_council import __version__
from model_council.config import Member, load_council, redact_proxy
from model_council.materials import (Loaded, Material, MaterialError, Policy,
                                     get_policy, render_for_seat, set_policy)
from model_council.materials import load as load_materials
from model_council.providers import Answer, ask_member, env_proxy, probe_member

COUNCIL = load_council()

# Each extra round costs another full call per member, and the prompt carries
# every previous answer, so it costs more than the one before it. Two is the
# useful shape; three is the most anyone should pay by accident.
MAX_ROUNDS = 3

# Constrain `model` to the ids this install actually has, so the caller cannot
# invent one: the schema carries an enum, and a wrong value is rejected with the
# valid values listed before it ever reaches a tool body. An empty roster has no
# valid values at all, and Literal[()] is not a type, so it falls back to str.
ModelId = Literal[tuple(COUNCIL.ids)] if COUNCIL.ids else str  # type: ignore[valid-type]

mcp = MCPServer("model-council", version=__version__)


def _roster() -> str:
    members = [m for m in COUNCIL.members.values() if m.enabled]
    if not members:
        return "(none configured — see the server's README)"
    return ", ".join(m.id + ("" if m.configured else " [NOT CONFIGURED]") for m in members)


def _unknown(ids: list[str]) -> str:
    return (f"[unknown model id(s): {', '.join(ids)}. "
            f"Available: {_roster()}. Call list_council for details.]")


class Guest(BaseModel):
    """An answer the caller already holds, seated at the table.

    The point of the council is that answers get read by the other answerers.
    Anything you have obtained elsewhere — a subagent you spawned, your own
    first take, a colleague's opinion — is worth more inside that mechanism
    than beside it, and this is how it gets in.
    """

    label: str = Field(description="Who this answer is from, as it should appear "
                                   "in the transcript, e.g. 'Subagent'.")
    text: str = Field(description="The answer itself, verbatim. Do not summarise "
                                  "it: the members are shown exactly this text, "
                                  "and a summary is not the thing they would be "
                                  "critiquing.")
    weight: float = Field(default=1.0,
                          description="How far this answer carries, on the same "
                                      "scale as the members' weights. Default 1.")


def _seat_guests(guests: list[Guest] | None) -> list[Answer]:
    """Turn caller-supplied answers into seats at the table.

    They become ordinary `Answer`s over a `Member` flagged as a guest, so every
    downstream mechanism — labelling, the weight ranking, and above all the
    revision prompt — treats them as participants without knowing what they are.
    """
    seated = []
    for g in guests or []:
        text = (g.text or "").strip()
        if not text:
            continue                 # an empty chair is not a participant
        label = (g.label or "").strip() or "Guest"
        # revises=False: `ask_all` runs its own rounds, and nothing here can go
        # back and ask a guest for a second answer. Use `revise` if you want a
        # voice that keeps up with the members.
        seated.append(Answer(Member(id=f"guest:{label}", label=label,
                                    weight=g.weight, guest=True, revises=False),
                             text, ok=True))
    return seated


def _with_eyes(members: list[Member], docs: list[Loaded]) -> tuple[list[Member], list[Member]]:
    """Split the roster over a call that carries an image.

    A member configured `vision: false` is left out rather than sent the text
    alone. The alternative looks kinder and is worse: it would answer, in the
    same confident register as the members who saw the image, about material it
    was never shown — and that answer then goes into the next round as though it
    were informed. Sitting the seat out costs one opinion and says so.
    """
    if not any(d.is_image for d in docs):
        return members, []
    return [m for m in members if m.vision], [m for m in members if not m.vision]


def _blind_note(blind: list[Member]) -> str:
    who = ", ".join(m.label for m in blind)
    verb = "is" if len(blind) == 1 else "are"
    return (f"[{who} {verb} configured `vision: false` and sat out this call, which "
            f"carries an image. Set it true for a member whose model can see.]")


def _material_note(docs: list[Loaded], blind: list[Member]) -> str:
    """What the council was reading, for whoever reads the transcript."""
    note = (f"[material on the table: {'; '.join(d.describe() for d in docs)}. "
            f"Every member was handed the same bytes, ahead of the question.]")
    return f"{note}\n{_blind_note(blind)}" if blind else note


def _paths_note() -> str:
    """Whether `materials` may name a file here — said before a caller tries.

    The answer is a property of how this server was started, which the caller
    cannot see. Leaving it to be discovered costs a failed call to learn a fact
    that fits on one line.
    """
    policy = get_policy()
    if not policy.allow_paths:
        return f"refused ({policy.why or 'this server does not read its own host'}) — pass `text`"
    if policy.root is not None:
        return f"allowed under {policy.root}"
    return "allowed (any file this server can read)"


def _route(m: Member) -> str:
    """Which way this member's traffic leaves, in the words the config uses.

    Two members can differ here and nothing else in the table would show it, so
    a council where one seat is behind a proxy and the rest are not looks
    identical to one where they all are — until a call fails and only the reason
    differs. The password, if the URL carries one, is not printed.
    """
    if isinstance(m.proxy, str) and m.proxy:
        return redact_proxy(m.proxy)
    if m.proxy is False:
        return "direct"
    return "env" if env_proxy()[0] else "direct"


def _origin(m: Member) -> str:
    """What produced this answer, for its header."""
    return "supplied by the caller" if m.guest else m.model


def _labelled(a: Answer, weighted: bool) -> str:
    tag = f" · weight {a.member.weight:g}" if weighted else ""
    return f"===== {a.member.label} ({_origin(a.member)}){tag} =====\n{a.text}"


def _round_block(n: int, total: int, what: str, answers: list[Answer],
                 weighted: bool) -> str:
    body = "\n\n".join(_labelled(a, weighted) for a in answers)
    if total == 1:                       # a single round needs no ceremony
        return body
    return f"########## ROUND {n} of {total} — {what} ##########\n\n{body}"


def _guest_note(seated: list[Answer]) -> str:
    """Why the guests are missing from every round after the first.

    Only worth saying once there is more than one round: a guest that vanishes
    from round 2 looks like a member that dropped out or gave up its position,
    and the reader is about to weigh exactly that kind of movement.
    """
    who = ", ".join(a.member.label for a in seated)
    speaks = "speaks" if len(seated) == 1 else "speak"
    return (f"[{who} {speaks} once. The answer above was shown to every member in the "
            f"rounds that followed, and they argued with it — but a guest has nobody "
            f"to ask for a revision, so it does not reappear. Its absence from the "
            f"later rounds is not a position withdrawn.]")


def _weight_note(members: list[Member]) -> str:
    """How to read the weights, addressed to whoever is reading the answers.

    Two deliberate limits. It is produced only when the weights actually differ,
    because a paragraph explaining a distinction that does not exist is worse
    than silence. And it is shown to the caller, never to the members: a model
    told it is outranked stops arguing and agrees, which costs exactly the
    independent dissent the council was assembled to produce. `_revision_prompt`
    therefore carries the other answers and not their weights.
    """
    ranked = sorted(members, key=lambda m: (-m.weight, m.id))
    lines = [
        "[WEIGHTS — " + ", ".join(f"{m.label} {m.weight:g}" for m in ranked) + "]",
        "These are this council's standing priors on its members, not votes. Use them "
        "to break ties and to place the burden of proof: a claim only a low-weight "
        "member makes wants corroboration before you build on it. They do not settle "
        "an argument — a specific, checkable reason from the lowest weight beats a "
        "bare assertion from the highest, and members that agree are not independent "
        "evidence when they share a training lineage.",
    ]
    if any(m.weight == 0 for m in ranked):
        lines.append("Weight 0 is advisory: read the answer, do not count it as support.")
    return "\n".join(lines)


def _seat_letter(i: int) -> str:
    """An identity-free name for a seat, from its position at the table.

    Position, so it is stable: the same seat is the same letter to every member
    and in every round, which is what lets a model say "B was wrong about X" and
    be understood next round.
    """
    return chr(65 + i) if i < 26 else f"#{i + 1}"


def _seat_key(answers: list[Answer]) -> str:
    """Who was which letter — for the caller, who is the only one told."""
    pairs = ", ".join(f"{_seat_letter(i)} = {a.member.label}"
                      for i, a in enumerate(answers) if a.ok)
    return (f"[the members saw each other as letters, not names: {pairs}. A model "
            f"name is a standing signal about a source rather than evidence about "
            f"the question, so it is kept out of their prompts for the same reason "
            f"the weights are. You get the key because you are the one who has to "
            f"tell them apart.]")


def _revision_prompt(question: str, answers: list[Answer], me: Member, done: int) -> str:
    """What a member is shown in a later round.

    Members are stateless and share no conversation, so a second round is only a
    discussion if the first one travels with it: the question restated, this
    member's own answer, and every other member's — verbatim, and only the ones
    that actually came back. An error string is not an opinion worth critiquing.

    The other answers arrive as letters. A model name is a status signal exactly
    as a weight is — models hold strong priors about each other's makers — and
    withholding the number while printing the brand would leave the larger of the
    two channels open. What survives is the argument, which is what we wanted
    weighed. Note the limit honestly: this covers the labels we write, not a name
    a model puts inside its own prose, which travels on into the next round.
    """
    mine = next((a for a in answers if a.member.id == me.id and a.ok), None)
    blocks = [
        f"Several AI models were asked the same question independently. Round {done} is "
        f"finished and its answers are below; you are now in round {done + 1}. They are "
        f"labelled by letter rather than by model, deliberately: judge them by their "
        f"reasoning, since that is all you are being given to judge.",
        f"--- THE QUESTION ---\n{question}",
        f"--- YOUR OWN ROUND-{done} ANSWER ---\n"
        + (mine.text if mine else "(you did not answer)"),
    ]
    # A guest is presented as an answer like any other — nothing here says where
    # it came from or what it is worth, for the same reason weights and names are
    # withheld: a member should engage with the argument, not with its provenance.
    # Only the fact that it will not answer back is stated, so its silence in the
    # next round does not read as a position abandoned.
    for i, a in enumerate(answers):
        if not a.ok or a.member.id == me.id:
            continue
        seat = _seat_letter(i)
        origin = (f"ROUND-{done} ANSWER {seat}" if a.member.revises
                  else f"ANSWER {seat} (does not revise between rounds)")
        blocks.append(f"--- {origin} ---\n{a.text}")
    blocks.append(
        "Now give your final answer to the question above. Weigh the other answers on "
        "their merits: take what is right, correct anything you got wrong, and say "
        "plainly where you still disagree and why. Agreement is not the goal — do not "
        "abandon a position you believe is correct just because you are outnumbered, "
        "and do not manufacture disagreement either. Answer in full: your reply is read "
        "on its own, not as a diff against the previous round."
    )
    return "\n\n".join(blocks)


# The one paragraph four tools need to say the same way. Written for the caller
# that is a model: it explains the cost of the alternative, because pasting a
# document into `prompt` is the thing that looks free and is not.
_MATERIAL_DOC = """`materials` hands the council the thing the question is about — a spec, a log, \
a diff, a screenshot — instead of you pasting it into `prompt`. Give \
`{path, label}` for a file, or `{text, label}` for something with no file behind \
it. Prefer a path: the members are then handed the file's exact bytes rather \
than your reproduction of them, you do not spend a copy of the whole document \
writing this call, and every member and every round get an identical copy, which \
is both what makes their answers comparable and what an endpoint's cache can \
match. Images go this way too, and are the case that matters most: describe a \
screenshot in prose and every member inherits the same description, so anything \
you misread is misread by the whole council at once."""


# --------------------------------------------------------------------------- #
# MCP tools
#
# The valid ids live in the `model` schema as an enum, not in the prose, so they
# are stated once and enforced rather than merely suggested. What each member
# actually is — endpoint, wire format, whether it is ready — belongs in
# list_council, which the caller reads only when it needs to choose.
# --------------------------------------------------------------------------- #
@mcp.tool(description="""Ask ONE member of the council a question and return its answer.

`model` is the member's short id; the schema lists the ones this council has, and
`list_council` describes them.

The member is stateless and cannot see this conversation, so `prompt` must carry
everything it needs — including any other model's answer you want it to critique.
Optionally set `system` to steer its role or output format.

""" + _MATERIAL_DOC)
async def ask(model: ModelId, prompt: str, system: str | None = None,  # type: ignore[valid-type]
              materials: list[Material] | None = None) -> str:
    # The enum normally rejects a bad id before we get here; this covers the
    # unconstrained fallback when no members are configured.
    m = COUNCIL.get(model)
    if m is None:
        return _unknown([model])
    try:
        docs = load_materials(materials)
    except MaterialError as e:
        return f"[{e}]"
    seeing, blind = _with_eyes([m], docs)
    if not seeing:
        return _blind_note(blind)
    return (await ask_member(m, prompt, system, docs)).text


@mcp.tool(description="""Ask several members the SAME prompt in parallel, returning
their answers side by side and labeled by model.

By default every configured member answers. Pass `models` to ask only some of
them; the schema lists the valid ids.

`rounds` (1-3, default 1) runs a real discussion. With `rounds=2` the members
first answer independently, then each is asked again — this time carrying the
question plus every answer from round 1, its own and the others', verbatim — and
asked to revise. Members are stateless, so carrying the previous round back to
them is the whole mechanism: without it a second round is just the same question
asked twice. The transcript returns round by round, so you can see who moved and
who held their ground.

One round is a survey of opinion. Two is worth the extra latency and tokens when
the answers are likely to disagree and the disagreement is the interesting part.

`rounds` is chosen before anything has been asked, which is the one thing wrong
with it: you commit to a second round without having read the first, and a
council that turns out to agree costs exactly what one still arguing would. When
you would rather look first, ask with `rounds=1` and then call `revise` with the
answers — it runs one more round on demand, as many times as you judge it worth,
and it has no ceiling. Reading before you buy is usually the cheaper of the two:
one more round is another full call per member, while `revise` costs you only the
answers written back into it.

`guests` seats answers you already have. If you spawned a subagent on this same
question, or formed your own view first, pass it here as {label, text} and it
joins the table: it appears in round 1 beside the members, and from round 2 the
members are shown it verbatim and argue with it. This is the difference between
an answer that is in the discussion and one that is merely next to it — without
it, the models never learn your subagent had an opinion. Pass the text verbatim,
not a summary; a summary is not what you want critiqued.

A guest speaks once and does not revise, so it appears in round 1 only. The
transcript says so, and says it is not a retraction.

Members may carry different weights — how much this council trusts each one. When
they do, every answer is labeled with its weight and the transcript ends with the
ranking and how to read it. The members are never told each other's weights; a
model told it is outranked stops arguing, and its dissent is what you came for.

""" + _MATERIAL_DOC + """ It is carried into every round for you, so a discussion
about one document costs you the path once.""")
async def ask_all(prompt: str, models: list[ModelId] | None = None,  # type: ignore[valid-type]
                  system: str | None = None, rounds: int = 1,
                  guests: list[Guest] | None = None,
                  materials: list[Material] | None = None) -> str:
    members, unknown = COUNCIL.resolve(models)
    seated = _seat_guests(guests)
    # Before anything is asked: a council given a question about a document it
    # never received would answer it anyway, at length, and read as though it
    # had. That failure has to stop the call, not degrade it.
    try:
        docs = load_materials(materials)
    except MaterialError as e:
        return f"[{e}]"
    members, blind = _with_eyes(members, docs)
    if not members and blind:
        return (f"[this call carries an image and every member asked for is configured "
                f"`vision: false`, so there is nobody left to show it to. Set it true "
                f"for a member whose model can see, or ask without the image.]")
    if not members:
        if unknown:
            return _unknown(unknown)
        if seated:
            return ("[no council members are configured, so there is nobody to put "
                    "these answers in front of. A guest is seated to be argued with; "
                    "with an empty roster this would only hand you back your own text.]")
        return "[no council members are configured]"

    # Guests are weighed on the same scale as members, so the ranking has to see
    # them. Weights are relative: a table that agrees on one number says nothing.
    everyone = members + [a.member for a in seated]
    weighted = len({m.weight for m in everyone}) > 1

    total = max(1, min(rounds, MAX_ROUNDS))
    answers = await asyncio.gather(*(ask_member(m, prompt, system, docs) for m in members))

    # `table` is what the next round is shown: this round's member answers plus
    # the guests, who persist unchanged. `answers` alone is what each round's
    # block prints, so a guest is not reprinted as though it had spoken again.
    table = list(answers) + seated
    parts = [_round_block(1, total, "independent answers", table, weighted)]
    note = ""

    for done in range(1, total):
        answered = sum(a.ok for a in table)
        if answered < 2:
            note = (f"\n\n[stopped after round {done}: a discussion needs at least two "
                    f"answers to put in front of each other, and this round produced "
                    f"{answered}.]")
            break
        # The material goes again with every round: the members are stateless, so
        # "the document from last time" does not exist for them. It is the same
        # bytes in the same position each time, which is the arrangement an
        # endpoint that caches by prefix can actually reuse.
        answers = await asyncio.gather(
            *(ask_member(m, _revision_prompt(prompt, table, m, done), system, docs)
              for m in members))
        table = list(answers) + seated
        parts.append(_round_block(done + 1, total,
                                  "revised after reading the other answers", answers,
                                  weighted))

    out = "\n\n".join(parts) + note
    # A caller that asked for the maximum and got it has been shown a ceiling,
    # and the obvious reading of a ceiling is that the discussion has none left.
    # It is a ceiling on what one call will spend, and the correction belongs
    # where the impression was made.
    if len(parts) == MAX_ROUNDS and sum(a.ok for a in table) >= 2:
        out += (f"\n\n[{MAX_ROUNDS} rounds is the most `ask_all` runs in one call, not the "
                f"most this discussion can have. `revise(prompt, answers=[the answers "
                f"above, verbatim], round={MAX_ROUNDS})` runs another, and has no ceiling "
                f"of its own — the one here exists because this call commits to its "
                f"rounds before you have read any of them.]")
    if docs:
        out = f"{_material_note(docs, blind)}\n\n{out}"
    if len(parts) > 1:
        # Only after a revision round: round 1 is answered blind, so no member
        # has seen another's letter yet and there is no key to explain.
        out += f"\n\n{_seat_key(table)}"
    if seated and total > 1:
        out += f"\n\n{_guest_note(seated)}"
    if weighted:
        out += f"\n\n{_weight_note(everyone)}"
    return out + (f"\n\n{_unknown(unknown)}" if unknown else "")


class PriorAnswer(BaseModel):
    """One answer from the round that just finished, on its way into the next."""

    text: str = Field(description="The answer, verbatim.")
    model: str | None = Field(
        default=None,
        description="The member id this answer came from, when it came from one. "
                    "That member is shown this back as its own answer, which is "
                    "what lets it revise rather than start over. Leave unset for "
                    "an answer from outside the roster — your subagent, or your "
                    "own — and give `label` instead.")
    label: str | None = Field(
        default=None,
        description="Name for an answer from outside the roster, e.g. 'Subagent'. "
                    "Ignored when `model` is set.")
    weight: float = Field(
        default=1.0,
        description="How far an outside answer carries, on the same scale as the "
                    "members' weights. Ignored when `model` is set — a member's "
                    "weight comes from the council's configuration.")


def _prior(answers: list[PriorAnswer]) -> tuple[list[Answer], list[str]]:
    """Rebuild a finished round from what the caller kept.

    An entry naming a member is restored as that member, identity and all, so
    the revision prompt can hand it back as its own answer. Everything else
    becomes an outside voice.
    """
    table: list[Answer] = []
    unknown: list[str] = []
    for a in answers:
        text = (a.text or "").strip()
        if not text:
            continue                       # an empty answer is not a position
        member = COUNCIL.get(a.model) if a.model else None
        if a.model and member is None:
            unknown.append(a.model)
        if member is None:
            label = (a.label or "").strip() or (a.model or "").strip() or "Guest"
            member = Member(id=f"guest:{label}", label=label, weight=a.weight,
                            guest=True)
        table.append(Answer(member, text, ok=True))
    return table, unknown


@mcp.tool(description="""Run ONE more round of an existing discussion: show every
member what was said last round and ask it to revise.

This is `ask_all`'s rounds turned inside out, so that you can drive them. Two
reasons to want that.

**You would rather decide after reading.** `ask_all(rounds=2)` commits to the
second round before the first one exists. Ask with `rounds=1`, read what comes
back, and call this only if the disagreement is worth another call per member —
or call it three more times if they are still moving. There is no round ceiling
here, unlike `ask_all`, because every round is one you chose to pay for.

**A voice in the discussion is one only you can produce** — a subagent you
spawned, or your own answer — and you want it to be a full member rather than a
one-off: something that answers, reads the others, and revises alongside them.

The loop, in the second case:

  1. `ask_all(prompt)`               — the members answer round 1
     ...and you produce your subagent's round-1 answer yourself
  2. `revise(prompt, answers=[...])` — pass EVERY round-1 answer, the members'
     and your subagent's; the members come back revised
     ...and you re-run your subagent on the same material
  3. repeat as long as it is still moving

Each entry in `answers` is `{text, model}` for a member's own answer, or
`{text, label}` for an outside one. Naming the member matters: that is what lets
it see its previous answer as its own and revise, instead of answering fresh.

`round` is which round the answers you are passing came from, so the members are
told where they are. Pass answers verbatim, never summarised.

Pass the same `materials` you passed to `ask_all`, every round. The members are
stateless: a document they were shown last round does not exist for them in this
one, and a council revising from memory it does not have will revise from the
other answers alone.""")
async def revise(prompt: str, answers: list[PriorAnswer],
                 models: list[ModelId] | None = None,  # type: ignore[valid-type]
                 system: str | None = None, round: int = 1,
                 materials: list[Material] | None = None) -> str:
    members, unknown_ids = COUNCIL.resolve(models)
    try:
        docs = load_materials(materials)
    except MaterialError as e:
        return f"[{e}]"
    members, blind = _with_eyes(members, docs)
    if not members and blind:
        return (f"[this call carries an image and every member asked for is configured "
                f"`vision: false`, so there is nobody left to show it to.]")
    if not members:
        return _unknown(unknown_ids) if unknown_ids else "[no council members are configured]"

    table, unknown_prior = _prior(answers)
    if len(table) < 2:
        return (f"[a revision round needs at least two answers to put in front of each "
                f"other, and {len(table)} was supplied. Pass every answer from the "
                f"previous round — each member's and your own — not just the ones you "
                f"want critiqued.]")

    done = max(1, round)
    revised = await asyncio.gather(
        *(ask_member(m, _revision_prompt(prompt, table, m, done), system, docs)
          for m in members))

    everyone = members + [a.member for a in table if a.member.guest]
    weighted = len({m.weight for m in everyone}) > 1
    seen = ", ".join(a.member.label for a in table)
    out = [f"########## ROUND {done + 1} — revised after reading round {done} "
           f"##########",
           f"[at the table: {seen}]",
           "",
           "\n\n".join(_labelled(a, weighted) for a in revised)]
    if docs:
        out.insert(2, _material_note(docs, blind))
    body = "\n".join(out)

    # The seats this server cannot ask. Said in the output and not only in the
    # docs, because the failure is silent: a subagent re-run on the original
    # question repeats itself, the transcript looks like a discussion, and
    # nothing marks the round where that seat stopped taking part.
    body += f"\n\n{_seat_key(table)}"

    outsiders = [a.member.label for a in table if a.member.guest]
    if outsiders:
        who = ", ".join(outsiders)
        body += (f"\n\n[{who} did not answer above — that seat is yours to re-run, and "
                 f"this round is not finished until you do. Give it the prompt from "
                 f"`revision_prompt(seat=\"{outsiders[0]}\", ...)`, which is what the "
                 f"members were given; the original question alone will only get you "
                 f"its previous answer again.]")

    if weighted:
        body += f"\n\n{_weight_note(everyone)}"
    if unknown_prior:
        body += (f"\n\n[these ids are not on this council: {', '.join(unknown_prior)}. "
                 f"Their answers were carried in as outside voices, which means those "
                 f"members were not shown their own previous answer. Available: "
                 f"{_roster()}.]")
    return body + (f"\n\n{_unknown(unknown_ids)}" if unknown_ids else "")


@mcp.tool(description="""Build the exact prompt a seat should be given for the next
round — for the one seat this server cannot ask itself: yours.

`revise` asks the members. Your subagent is yours to re-run, and how you prompt it
decides whether it revises at all: handed the original question again, it will
reproduce its previous answer, and nothing in the transcript will show that the
seat stopped participating. Handed this, it revises on exactly the terms the
members did — same framing, same instruction to hold a position it still believes
against the majority, which is the sentence that keeps a council from collapsing
into agreement.

Pass the same `prompt`, `answers`, `round` and `materials` you are passing to
`revise`, plus `seat`: the label (or member id) of the seat to write for. Cheap
and local — makes no network calls, so run it alongside `revise` rather than
after it.

Material is named in the prompt rather than pasted into it, at the position the
members were given it. Open those files for your seat before you hand it the
rest: a seat that revises without the document is arguing about something it has
not read, and the transcript will not show it.""")
async def revision_prompt(prompt: str, answers: list[PriorAnswer], seat: str,
                          round: int = 1,
                          materials: list[Material] | None = None) -> str:
    table, _ = _prior(answers)
    if not table:
        return "[no previous answers were supplied, so there is nothing to revise from]"
    try:
        docs = load_materials(materials)
    except MaterialError as e:
        return f"[{e}]"

    key = (seat or "").strip().lower()
    if not key:
        return "[name the seat to write for, e.g. seat='Subagent']"

    me = next((a.member for a in table
               if a.member.id.lower() == key or a.member.label.lower() == key), None)
    if me is None:
        # A seat that sat out the last round still gets to join this one; the
        # prompt tells it so rather than pretending it had said something.
        me = Member(id=f"guest:{seat}", label=seat.strip(), guest=True)

    written = _revision_prompt(prompt, table, me, max(1, round))
    material = render_for_seat(docs)
    return f"{material}\n\n{written}" if material else written


@mcp.tool()
async def list_council() -> str:
    """List the council's members: their ids, labels, target models, weights, wire
    format, endpoint, call budget, and whether each one is ready to answer.

    `weight` is how much this council trusts each member, relative to the others;
    everyone is 1 unless the roster says otherwise, and `ask_all` reports it
    alongside the answers whenever they differ.

    `tries` is how many attempts a call gets and how long each may take, so a
    member that is slow or that keeps being retried is visible here.

    `sees` is whether this member may be shown an image in `materials`. The line
    above the table says whether this server will read `materials` paths at all,
    which depends on how its operator runs it.

    `route` is how this member reaches its endpoint — `env` follows the proxy in
    the server's environment, `direct` ignores it, and a URL is a proxy set for
    that member alone. It appears only when the members can differ; passwords in
    a proxy URL are masked.

    Cheap and local — makes no network calls. Use this to find out which ids you
    may pass to `ask` and `ask_all`, or to explain a configuration problem.
    """
    rows = [("id", "label", "model", "weight", "format", "sees", "route", "endpoint",
             "tries", "status")]
    for m in COUNCIL.members.values():
        status = "ready" if m.configured else f"missing {', '.join(m.missing)}"
        if not m.enabled:
            status = f"disabled — {m.disabled_reason}" if m.disabled_reason else "disabled"
        rows.append((m.id, m.label, m.model or "-", f"{m.weight:g}", m.format,
                     "text+img" if m.vision else "text", _route(m),
                     m.base_url or "-", f"{m.retries + 1} × {m.timeout:g}s", status))

    # The route column earns its width only when the routes can differ. With no
    # proxy configured anywhere and none in the environment, every member says
    # "direct" and the column is a wall of the same word.
    var, val = env_proxy()
    if not var and all(m.proxy is None for m in COUNCIL.members.values()):
        rows = [r[:6] + r[7:] for r in rows]

    widths = [max(len(r[i]) for r in rows) for i in range(len(rows[0]))]
    table = "\n".join("  ".join(c.ljust(w) for c, w in zip(r, widths)).rstrip() for r in rows)

    out = [f"config source: {COUNCIL.source}", f"material paths: {_paths_note()}"]
    if var:
        out.append(f"network: {var}={redact_proxy(val)} in this server's environment "
                   f"— the members whose route is 'env' go through it")
    out += ["", table]
    if COUNCIL.warnings:
        out += ["", "warnings:"] + [f"  - {w}" for w in COUNCIL.warnings]
    return "\n".join(out)


@mcp.tool(description="""Ask a member's endpoint which model ids it actually exposes,
by calling its /models route.

Pass `model` to probe one member, or omit it to probe every configured member.

Use this when a call fails with an unknown-model error, or to discover what else
a provider offers — model ids move fast.""")
async def probe_models(model: ModelId | None = None) -> str:  # type: ignore[valid-type]
    members, unknown = COUNCIL.resolve([model] if model else None)
    if not members:
        return _unknown(unknown) if unknown else "[no council members are configured]"
    lines: list[str] = []
    for m in members:
        if not m.configured:
            lines.append(f"{m.label}: not configured (missing {', '.join(m.missing)})")
            continue
        try:
            lines.append(f"{m.label} (using: {m.model}) -> {await probe_member(m)}")
        except Exception as e:  # noqa: BLE001
            lines.append(f"{m.label}: [could not list models — {type(e).__name__}: {e}]")
    return "\n".join(lines)


@mcp.custom_route("/healthz", methods=["GET"])
async def healthz(request: Request) -> Response:
    """Is the deployment up, and does it have a council to offer.

    Counts only, never the roster: an unauthenticated endpoint that names which
    providers an organisation pays for is a small intelligence leak, and any
    caller entitled to that detail can call `list_council` for it.

    A server with no configured member answers 503. It is running, but every
    tool it exposes would return a configuration error, and a deploy that is
    broken in exactly that way should fail its health check rather than sit
    there looking healthy.
    """
    ready = sum(m.configured and m.enabled for m in COUNCIL.members.values())
    return JSONResponse(
        {"status": "ok" if ready else "no members configured",
         "version": __version__,
         "members": {"ready": ready, "total": len(COUNCIL.members)}},
        status_code=200 if ready else 503,
    )


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
LOCAL_BINDS = ("127.0.0.1", "localhost", "::1")


def _env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() not in ("", "0", "false", "no", "off")


def _parse_args(argv: list[str] | None) -> tuple[argparse.ArgumentParser, argparse.Namespace]:
    p = argparse.ArgumentParser(
        prog="model-council-mcp",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="Model Council — an MCP server that seats other LLMs at your table.",
        epilog="""\
Every option also reads an environment variable, so a container or a systemd
unit can configure the server without rewriting its command line.

  stdio (default)   one copy per user, launched by their MCP client
  --http            one deployment, one set of keys, many users

examples:
  model-council-mcp
  model-council-mcp --http --host 0.0.0.0 --allow private
  model-council-mcp --http --host 0.0.0.0 --allow 10.20.0.0/16,10.30.1.5
  model-council-mcp --http --allow private --trust-proxy 10.0.0.9
  model-council-mcp --http --allow private --materials-root /srv/council/material
""")
    p.add_argument("--http", action="store_true", default=_env_flag("COUNCIL_HTTP"),
                   help="listen over Streamable HTTP instead of stdio [COUNCIL_HTTP]")
    p.add_argument("--host", default=os.environ.get("COUNCIL_HTTP_HOST", "127.0.0.1"),
                   help="address to bind (default: %(default)s) [COUNCIL_HTTP_HOST]")
    p.add_argument("--port", type=int, default=int(os.environ.get("COUNCIL_HTTP_PORT", "8000")),
                   help="port to bind (default: %(default)s) [COUNCIL_HTTP_PORT]")
    p.add_argument("--path", default=os.environ.get("COUNCIL_HTTP_PATH", "/mcp"),
                   help="URL path for the MCP endpoint (default: %(default)s) "
                        "[COUNCIL_HTTP_PATH]")
    p.add_argument("--allow", default=os.environ.get("COUNCIL_ALLOW", ""),
                   help="networks that may call: CIDRs, addresses, or the names "
                        "'private', 'loopback', 'any'. Loopback is always allowed "
                        "[COUNCIL_ALLOW]")
    p.add_argument("--trust-proxy", default=os.environ.get("COUNCIL_TRUST_PROXY", ""),
                   help="reverse proxies whose X-Forwarded-For may be believed. "
                        "Without this the peer address is the client, which behind "
                        "a proxy is the proxy [COUNCIL_TRUST_PROXY]")
    p.add_argument("--allow-origin", action="append", metavar="ORIGIN",
                   default=[o for o in os.environ.get("COUNCIL_ALLOW_ORIGIN", "")
                            .replace(",", " ").split() if o],
                   help="permit browser requests from this Origin; repeatable. By "
                        "default any request carrying an Origin is refused "
                        "[COUNCIL_ALLOW_ORIGIN]")
    p.add_argument("--materials-root", metavar="DIR",
                   default=os.environ.get("COUNCIL_MATERIALS_ROOT", ""),
                   help="the one directory `materials` paths may name. Over HTTP "
                        "this is what allows paths at all — without it a networked "
                        "deployment refuses them and callers inline the text. Over "
                        "stdio, paths are already allowed and this narrows them "
                        "[COUNCIL_MATERIALS_ROOT]")
    p.add_argument("--version", action="version", version=f"model-council {__version__}")
    return p, p.parse_args(argv)


def _material_policy(parser: argparse.ArgumentParser,
                     args: argparse.Namespace) -> Policy:
    """Whether this process will read a caller's paths, decided once at startup.

    Over stdio, yes and without a flag — the caller launched this process, so it
    already reads everything this process can, exactly as a loopback bind needs
    no `--allow`. Over HTTP that reasoning is gone: the caller is whoever reached
    the port, a path would make this a file-read primitive on its host, and the
    file's contents would leave for an external provider. So HTTP refuses paths
    unless the operator names one directory to serve them from.
    """
    root = (getattr(args, "materials_root", "") or "").strip()
    if root:
        path = Path(root).expanduser()
        try:
            path = path.resolve(strict=True)
        except OSError:
            parser.error(f"--materials-root {path} does not exist")
        if not path.is_dir():
            parser.error(f"--materials-root {path} is not a directory")
        return Policy(allow_paths=True, root=path)
    if args.http:
        return Policy(allow_paths=False,
                      why="it serves over HTTP, where reading a caller's path would "
                          "make one shared council a way to read this host's files "
                          "and forward them to a provider")
    return Policy()


def _warn_about_config() -> None:
    for w in COUNCIL.warnings:
        print(f"model-council: warning: {w}", file=sys.stderr)
    if not any(m.configured for m in COUNCIL.members.values()):
        print(f"model-council: warning: no members are configured (source: {COUNCIL.source}) "
              f"— every tool will return a configuration error.\n"
              f"  Pass the settings in the MCP client's `env` block, or point "
              f"COUNCIL_CONFIG at a config file, or set COUNCIL_ENV_FILE to an "
              f"absolute .env path. A bare `.env` is only found when the server's "
              f"working directory contains it, which an MCP client does not "
              f"guarantee.", file=sys.stderr)


def _serve_http(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    """Run over Streamable HTTP, behind an address allowlist."""
    import uvicorn

    from model_council import access

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    log = logging.getLogger("model_council")

    try:
        allow = access.parse_networks(args.allow)
        trusted = access.parse_networks(args.trust_proxy)
    except ValueError as e:
        parser.error(str(e))

    # The refusal that makes the rest of this safe. This server answers with
    # shared provider keys and asks the caller for no credential of its own, so
    # reaching the port is the entire authorisation story — and an allowlist
    # that can be left out by forgetting a flag is not one. Binding loopback is
    # the one case that needs no flag, because the address already is the limit.
    if not allow and args.host not in LOCAL_BINDS:
        parser.error(
            f"--http --host {args.host} serves the network, and this server answers "
            f"with shared provider keys and no per-caller credential: whoever reaches "
            f"the port spends your quota. Say who may reach it with --allow "
            f"(e.g. --allow private, or --allow 10.20.0.0/16), or bind "
            f"--host 127.0.0.1 and put something in front of it.")

    if any(n.prefixlen == 0 for n in allow):
        log.warning("--allow includes the whole internet; the only thing limiting "
                    "who can spend this council's quota is whatever sits in front "
                    "of this process")
    if not trusted:
        log.info("no --trust-proxy set: X-Forwarded-For is ignored and the peer "
                 "address decides. Behind a reverse proxy, that is the proxy.")
    policy = _material_policy(parser, args)
    set_policy(policy)
    if policy.root is not None:
        log.info("materials: callers may name files under %s, and this process "
                 "forwards what it reads to a provider", policy.root)
    else:
        log.info("materials: paths are refused and callers must inline the text; "
                 "--materials-root DIR opens one directory")

    _warn_about_config()

    app = mcp.streamable_http_app(streamable_http_path=args.path, stateless_http=True)
    gated = access.ClientGate(app, allow=allow, trusted_proxies=trusted,
                              allowed_origins=args.allow_origin)

    log.info("model-council %s serving %s on http://%s:%d%s",
             __version__, ", ".join(COUNCIL.ids) or "(no members)",
             args.host, args.port, args.path)

    uvicorn.run(
        gated,
        host=args.host,
        port=args.port,
        # uvicorn rewrites scope["client"] from X-Forwarded-For by default, for
        # peers in its own separate trust list. Two mechanisms deciding the same
        # thing means --trust-proxy would sometimes not be what decided it, so
        # this one is turned off and ClientGate is left as the only authority.
        proxy_headers=False,
    )


def main(argv: list[str] | None = None) -> None:
    """Console-script entry point."""
    parser, args = _parse_args(argv)
    if args.http:
        _serve_http(parser, args)
        return
    set_policy(_material_policy(parser, args))
    _warn_about_config()
    mcp.run()


if __name__ == "__main__":
    main()
