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
from typing import Literal

from mcp.server import MCPServer
from mcp.server.mcpserver.context import Context
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

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


def _origin(m: Member) -> str:
    """What actually answers for this seat.

    Naming the client explicitly matters for a sampling member: the reader of
    the transcript is that client, and an answer it produced itself is not an
    outside opinion. Printing the model id there would hide exactly that.
    """
    if not m.is_sampling:
        return m.model
    return "this client" + (f", hinted {m.model}" if m.model else "")


def _labelled(a: Answer, weighted: bool) -> str:
    tag = f" · weight {a.member.weight:g}" if weighted else ""
    return f"===== {a.member.label} ({_origin(a.member)}){tag} =====\n{a.text}"


def _round_block(n: int, total: int, what: str, answers: list[Answer],
                 weighted: bool) -> str:
    body = "\n\n".join(_labelled(a, weighted) for a in answers)
    if total == 1:                       # a single round needs no ceremony
        return body
    return f"########## ROUND {n} of {total} — {what} ##########\n\n{body}"


def _session(ctx: Context | None):
    """The live MCP session, when there is one. `None` on a direct Python call."""
    return getattr(ctx, "session", None)


def _self_note(members: list[Member]) -> str:
    """Told to the caller when one of the seats was the caller's own model.

    A sampling member is a fresh instance with none of this conversation, which
    makes it a real second look — but it is not a second opinion. Whoever reads
    this transcript is the same model that produced that answer, and counting
    its agreement as corroboration is the one mistake this seat makes easy.
    """
    seats = [m.label for m in members if m.is_sampling]
    if not seats:
        return ""
    who = seats[0] if len(seats) == 1 else ", ".join(seats[:-1]) + f" and {seats[-1]}"
    was = "was" if len(seats) == 1 else "were"
    return (f"[{who} {was} answered by your own model, over a sampling request back to "
            f"you: a fresh instance carrying none of this conversation, but the same "
            f"model reasoning again. Read it as a second look, not a second opinion — "
            f"where it agrees with you, that is one view restated, not confirmation.]")


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
async def ask(model: ModelId, prompt: str, system: str | None = None,  # type: ignore[valid-type]
              *, ctx: Context | None = None) -> str:
    # The enum normally rejects a bad id before we get here; this covers the
    # unconstrained fallback when no members are configured.
    m = COUNCIL.get(model)
    if m is None:
        return _unknown([model])
    answer = await ask_member(m, prompt, system, session=_session(ctx))
    return answer.text + (f"\n\n{_self_note([m])}" if m.is_sampling and answer.ok else "")


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

Members may carry different weights — how much this council trusts each one. When
they do, every answer is labeled with its weight and the transcript ends with the
ranking and how to read it. The members are never told each other's weights; a
model told it is outranked stops arguing, and its dissent is what you came for.

A member configured with `format: "sampling"` is answered by your own model, over
a request back to this client. It costs no key and no quota, and it carries none
of this conversation — but it is not an outside opinion, and the transcript says
so where it appears.""")
async def ask_all(prompt: str, models: list[ModelId] | None = None,  # type: ignore[valid-type]
                  system: str | None = None, rounds: int = 1,
                  *, ctx: Context | None = None) -> str:
    members, unknown = COUNCIL.resolve(models)
    if not members:
        return _unknown(unknown) if unknown else "[no council members are configured]"

    # Weights are relative, so a roster that agrees on one number says nothing —
    # and on the default roster that is every roster. Stay silent unless asked.
    weighted = len({m.weight for m in members}) > 1
    session = _session(ctx)

    total = max(1, min(rounds, MAX_ROUNDS))
    answers = await asyncio.gather(
        *(ask_member(m, prompt, system, session=session) for m in members))
    parts = [_round_block(1, total, "independent answers", answers, weighted)]
    note = ""
    # Which sampled seats actually spoke. A seat that only ever returned an
    # error must not draw the "this was you" note: there is no answer of ours
    # in the transcript to caveat, and saying otherwise would be a lie about
    # where the text came from.
    sampled = {a.member.id for a in answers if a.member.is_sampling and a.ok}

    for done in range(1, total):
        answered = sum(a.ok for a in answers)
        if answered < 2:
            note = (f"\n\n[stopped after round {done}: a discussion needs at least two "
                    f"answers to put in front of each other, and this round produced "
                    f"{answered}.]")
            break
        answers = await asyncio.gather(
            *(ask_member(m, _revision_prompt(prompt, answers, m, done), system,
                         session=session)
              for m in members))
        sampled |= {a.member.id for a in answers if a.member.is_sampling and a.ok}
        parts.append(_round_block(done + 1, total,
                                  "revised after reading the other answers", answers,
                                  weighted))

    out = "\n\n".join(parts) + note
    if sampled:
        out += f"\n\n{_self_note([m for m in members if m.id in sampled])}"
    if weighted:
        out += f"\n\n{_weight_note(members)}"
    return out + (f"\n\n{_unknown(unknown)}" if unknown else "")


@mcp.tool()
async def list_council() -> str:
    """List the council's members: their ids, labels, target models, weights, wire
    format, endpoint, call budget, and whether each one is ready to answer.

    `weight` is how much this council trusts each member, relative to the others;
    everyone is 1 unless the roster says otherwise, and `ask_all` reports it
    alongside the answers whenever they differ.

    A member whose format is `sampling` has no endpoint: it is answered by this
    MCP client's own model, and needs no key.

    `tries` is how many attempts a call gets and how long each may take, so a
    member that is slow or that keeps being retried is visible here.

    Cheap and local — makes no network calls. Use this to find out which ids you
    may pass to `ask` and `ask_all`, or to explain a configuration problem.
    """
    rows = [("id", "label", "model", "weight", "format", "endpoint", "tries", "status")]
    for m in COUNCIL.members.values():
        status = "ready" if m.configured else f"missing {', '.join(m.missing)}"
        if not m.enabled:
            status = f"disabled — {m.disabled_reason}" if m.disabled_reason else "disabled"
        endpoint = "(this MCP client)" if m.is_sampling else (m.base_url or "-")
        rows.append((m.id, m.label, m.model or "-", f"{m.weight:g}", m.format,
                     endpoint, f"{m.retries + 1} × {m.timeout:g}s", status))

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
    p.add_argument("--version", action="version", version=f"model-council {__version__}")
    return p, p.parse_args(argv)


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

    # Said at startup rather than left for the first failed answer: this server
    # runs stateless HTTP, which has no back-channel, and sampling is a request
    # travelling the other way. The seat cannot work here however it is configured.
    seats = [m.id for m in COUNCIL.members.values() if m.enabled and m.is_sampling]
    if seats:
        log.warning("sampling member(s) %s cannot answer over --http: this server "
                    "is stateless, so there is no way back to the client. They will "
                    "report that as their answer. Seat them on a stdio server.",
                    ", ".join(seats))

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
    _warn_about_config()
    mcp.run()


if __name__ == "__main__":
    main()
