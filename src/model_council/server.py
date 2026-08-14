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

from mcp.server.fastmcp import FastMCP

from model_council.config import Member, load_council
from model_council.providers import ask_member, probe_member

COUNCIL = load_council()

mcp = FastMCP("model-council")


def _roster() -> str:
    members = [m for m in COUNCIL.members.values() if m.enabled]
    if not members:
        return "(none configured — see the server's README)"
    return ", ".join(m.id + ("" if m.configured else " [NOT CONFIGURED]") for m in members)


def _unknown(ids: list[str]) -> str:
    return (f"[unknown model id(s): {', '.join(ids)}. "
            f"Available: {_roster()}. Call list_council for details.]")


def _labelled(m: Member, answer: str) -> str:
    return f"===== {m.label} ({m.model}) =====\n{answer}"


# --------------------------------------------------------------------------- #
# MCP tools
#
# The roster is injected into the descriptions at startup, so the model sees the
# ids it may actually pass without having to call list_council first.
# --------------------------------------------------------------------------- #
@mcp.tool(description=f"""Ask ONE member of the council a question and return its answer.

`model` is the member's short id. Available ids: {_roster()}

The member is stateless and cannot see this conversation, so `prompt` must carry
everything it needs — including any other model's answer you want it to critique.
Optionally set `system` to steer its role or output format.""")
async def ask(model: str, prompt: str, system: str | None = None) -> str:
    m = COUNCIL.get(model)
    if m is None:
        return _unknown([model])
    return await ask_member(m, prompt, system)


@mcp.tool(description=f"""Ask several members the SAME prompt in parallel, returning
their answers side by side and labeled by model.

By default every configured member answers. Pass `models` to ask only some of
them. Available ids: {_roster()}

Use this for the first round of a multi-model question. Afterwards you can
compare the answers yourself, or feed them back through `ask` so each member
revises its answer in light of the others.""")
async def ask_all(prompt: str, models: list[str] | None = None,
                  system: str | None = None) -> str:
    members, unknown = COUNCIL.resolve(models)
    if not members:
        return _unknown(unknown) if unknown else "[no council members are configured]"
    answers = await asyncio.gather(*(ask_member(m, prompt, system) for m in members))
    out = "\n\n".join(_labelled(m, a) for m, a in zip(members, answers))
    return out + (f"\n\n{_unknown(unknown)}" if unknown else "")


@mcp.tool()
async def list_council() -> str:
    """List the council's members: their ids, labels, target models, wire format,
    endpoint, and whether each one is ready to answer.

    Cheap and local — makes no network calls. Use this to find out which ids you
    may pass to `ask` and `ask_all`, or to explain a configuration problem.
    """
    rows = [("id", "label", "model", "format", "endpoint", "status")]
    for m in COUNCIL.members.values():
        status = "ready" if m.configured else f"missing {', '.join(m.missing)}"
        if not m.enabled:
            status = "disabled"
        rows.append((m.id, m.label, m.model or "-", m.format, m.base_url or "-", status))

    widths = [max(len(r[i]) for r in rows) for i in range(len(rows[0]))]
    table = "\n".join("  ".join(c.ljust(w) for c, w in zip(r, widths)).rstrip() for r in rows)

    out = [f"config source: {COUNCIL.source}", "", table]
    if COUNCIL.warnings:
        out += ["", "warnings:"] + [f"  - {w}" for w in COUNCIL.warnings]
    return "\n".join(out)


@mcp.tool(description=f"""Ask a member's endpoint which model ids it actually exposes,
by calling its /models route.

Pass `model` to probe one member, or omit it to probe every configured member.
Available ids: {_roster()}

Use this when a call fails with an unknown-model error, or to discover what else
a provider offers — model ids move fast.""")
async def probe_models(model: str | None = None) -> str:
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
