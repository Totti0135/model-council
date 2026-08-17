#!/usr/bin/env python3
"""Model Council — an MCP server.

Puts other LLMs on the table as tools, so your assistant can consult them,
compare their answers, and synthesize a conclusion inside one conversation.

Any number of models, from any mix of OpenAI-compatible and Anthropic-compatible
endpoints. See config.py for where the roster comes from.
"""
from __future__ import annotations

import asyncio
import sys
from typing import Literal

from mcp.server import MCPServer

from model_council import __version__
from model_council.config import Member, load_council
from model_council.providers import Answer, ask_member, probe_member

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


def _labelled(a: Answer) -> str:
    return f"===== {a.member.label} ({a.member.model}) =====\n{a.text}"


def _round_block(n: int, total: int, what: str, answers: list[Answer]) -> str:
    body = "\n\n".join(_labelled(a) for a in answers)
    if total == 1:                       # a single round needs no ceremony
        return body
    return f"########## ROUND {n} of {total} — {what} ##########\n\n{body}"


def _revision_prompt(question: str, answers: list[Answer], me: Member, done: int) -> str:
    """What a member is shown in a later round.

    Members are stateless and share no conversation, so a second round is only a
    discussion if the first one travels with it: the question restated, this
    member's own answer, and every other member's — verbatim, and only the ones
    that actually came back. An error string is not an opinion worth critiquing.
    """
    mine = next((a for a in answers if a.member.id == me.id and a.ok), None)
    blocks = [
        f"Several AI models were asked the same question independently. Round {done} is "
        f"finished and its answers are below; you are now in round {done + 1}.",
        f"--- THE QUESTION ---\n{question}",
        f"--- YOUR OWN ROUND-{done} ANSWER ---\n"
        + (mine.text if mine else "(you did not answer)"),
    ]
    blocks += [f"--- ROUND-{done} ANSWER FROM {a.member.label} ---\n{a.text}"
               for a in answers if a.ok and a.member.id != me.id]
    blocks.append(
        "Now give your final answer to the question above. Weigh the other answers on "
        "their merits: take what is right, correct anything you got wrong, and say "
        "plainly where you still disagree and why. Agreement is not the goal — do not "
        "abandon a position you believe is correct just because you are outnumbered, "
        "and do not manufacture disagreement either. Answer in full: your reply is read "
        "on its own, not as a diff against the previous round."
    )
    return "\n\n".join(blocks)


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
Optionally set `system` to steer its role or output format.""")
async def ask(model: ModelId, prompt: str, system: str | None = None) -> str:  # type: ignore[valid-type]
    # The enum normally rejects a bad id before we get here; this covers the
    # unconstrained fallback when no members are configured.
    m = COUNCIL.get(model)
    if m is None:
        return _unknown([model])
    return (await ask_member(m, prompt, system)).text


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
the answers are likely to disagree and the disagreement is the interesting part.""")
async def ask_all(prompt: str, models: list[ModelId] | None = None,  # type: ignore[valid-type]
                  system: str | None = None, rounds: int = 1) -> str:
    members, unknown = COUNCIL.resolve(models)
    if not members:
        return _unknown(unknown) if unknown else "[no council members are configured]"

    total = max(1, min(rounds, MAX_ROUNDS))
    answers = await asyncio.gather(*(ask_member(m, prompt, system) for m in members))
    parts = [_round_block(1, total, "independent answers", answers)]
    note = ""

    for done in range(1, total):
        answered = sum(a.ok for a in answers)
        if answered < 2:
            note = (f"\n\n[stopped after round {done}: a discussion needs at least two "
                    f"answers to put in front of each other, and this round produced "
                    f"{answered}.]")
            break
        answers = await asyncio.gather(
            *(ask_member(m, _revision_prompt(prompt, answers, m, done), system)
              for m in members))
        parts.append(_round_block(done + 1, total,
                                  "revised after reading the other answers", answers))

    out = "\n\n".join(parts) + note
    return out + (f"\n\n{_unknown(unknown)}" if unknown else "")


@mcp.tool()
async def list_council() -> str:
    """List the council's members: their ids, labels, target models, wire format,
    endpoint, call budget, and whether each one is ready to answer.

    `tries` is how many attempts a call gets and how long each may take, so a
    member that is slow or that keeps being retried is visible here.

    Cheap and local — makes no network calls. Use this to find out which ids you
    may pass to `ask` and `ask_all`, or to explain a configuration problem.
    """
    rows = [("id", "label", "model", "format", "endpoint", "tries", "status")]
    for m in COUNCIL.members.values():
        status = "ready" if m.configured else f"missing {', '.join(m.missing)}"
        if not m.enabled:
            status = f"disabled — {m.disabled_reason}" if m.disabled_reason else "disabled"
        rows.append((m.id, m.label, m.model or "-", m.format, m.base_url or "-",
                     f"{m.retries + 1} × {m.timeout:g}s", status))

    widths = [max(len(r[i]) for r in rows) for i in range(len(rows[0]))]
    table = "\n".join("  ".join(c.ljust(w) for c, w in zip(r, widths)).rstrip() for r in rows)

    out = [f"config source: {COUNCIL.source}", "", table]
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


def main() -> None:
    """Console-script entry point: run over stdio, which is what MCP clients launch."""
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
    mcp.run()


if __name__ == "__main__":
    main()
