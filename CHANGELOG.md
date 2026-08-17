# Changelog

## 0.4.0

**`ask_all` can run the discussion itself: `rounds=2`.** Round 1 is the usual
parallel ask; round 2 goes back to every member carrying the question plus each
answer from round 1 — its own and the others', verbatim — and asks it to revise.
Carrying the previous round back is the entire mechanism: members are stateless,
so a second round without it is just the same question asked twice. This was
possible before by hand, by pasting answers into `ask` prompts, and now it is one
argument. Up to 3 rounds; the transcript returns round by round; failed answers
are left out of what the others are shown, since an error message is not an
opinion worth critiquing. `rounds=1` is the default and its output is byte-for-byte
what it always was.

**Transient failures are retried instead of reported.** HTTP 429 and 5xx, and
dropped or timed-out connections, get 2 more attempts by default, backing off
exponentially from 1s with jitter. A `Retry-After` header beats that curve, and
one asking for longer than 30s ends the call with what it asked for rather than
sitting on it. Failures that will not change — 401, 404, a malformed response
body — are still reported on the first try, because asking again only spends the
same quota to be told the same thing. Tunable per member or per provider with
`retries` and `retry_backoff` (or `<ID>_RETRIES` / `COUNCIL_RETRIES`); `retries: 0`
restores the old behaviour.

Note the interaction with `timeout`, which is per attempt: the default budget is
now up to 3 × 180s rather than a flat 180s. `list_council` gained a `tries`
column showing each member's, and a member that exhausts its attempts says
`gave up after N attempts`.

**A response with no usable content is now a failure, not an answer.** An empty
completion or an unparseable body used to be returned as the member's answer,
which meant it could be fed to another model as an opinion. It is now attributed
(`[GPT-5 empty response]` rather than a bare `[empty response]`) and excluded
from later rounds.

## 0.3.0

**Ships as a desktop extension.** `model-council-<version>.mcpb` is attached to
each GitHub release; Claude Desktop installs it in one click and collects the
endpoints and keys through its own form, so the keys go to the OS keychain
rather than sitting in plaintext in a config file. The bundle uses the MCPB `uv`
runtime, so it is ~17 kB and needs no Python on the machine — dependencies are
resolved on the host from `pyproject.toml`. The form configures two seats;
point its "Config file" field at a JSON config for anything larger.

**Behaviour change: explicit configuration now beats a discovered file.** The
order is COUNCIL_CONFIG, then COUNCIL_MODELS, then a config file at the default
path, then the built-in roster. Previously a file at
`~/.config/model-council/config.json` won even when the client had passed
COUNCIL_MODELS, which made the extension's form look broken: you fill it in,
nothing you typed takes effect, and nothing says why. If you relied on a
discovered file overriding an environment roster, unset COUNCIL_MODELS or point
COUNCIL_CONFIG at the file.

## 0.2.1

Listed on the official MCP Registry as `io.github.Totti0135/model-council`, so
clients that browse the registry can find the server instead of being told about
it. No code changes: this adds `server.json`, the `mcp-name` ownership marker the
registry reads out of the PyPI README, and a release job that publishes the
listing after the PyPI upload is visible.

## 0.2.0

**Requires the mcp 2.x SDK.** 2.0 removed `mcp.server.fastmcp` and replaced
`FastMCP` with `MCPServer`; the server is written against the new class, and the
dependency is now `mcp>=2,<3`. The tools, their arguments and the wire protocol
are unchanged, so no configuration has to move. If you must stay on the 1.x SDK,
pin `model-council-mcp==0.1.1`.

**`model` is now a schema enum instead of a free string.** The ids this council
has are built into the tool schema at startup, so a wrong one is rejected —
with the valid values listed — before any tool body runs. The roster no longer
has to be restated in the tool descriptions, which keeps them a fixed size no
matter how many members you configure; `list_council` remains the place to look
up what each member actually is.

**New `proxy` field on providers and members.** Omit it to follow
`HTTP_PROXY`/`HTTPS_PROXY` as before, set `false` to connect directly, or give a
URL to route that member through a specific proxy. Without this, a member on a
network the system proxy cannot reach — an internal gateway behind a VPN — fails
with a bare `ConnectError` that never mentions a proxy. Connection errors now
also say when a proxy is in play and what to do about it.

## 0.1.1

**Security fix.** A member that named a `provider` and also overrode `base_url`
inherited that provider's `api_key`, so the key was sent to whatever host the
member named — with no warning that it had happened. Affects 0.1.0.

Not remotely exploitable: it takes a config you wrote yourself to trigger. But it
is silent, and the consequence is a credential delivered to the wrong endpoint,
so treat any key used in such a config as disclosed to that host and rotate it.

`base_url`, `api_key` and `format` are now one atomic unit. A member either names
a provider and takes the whole connection, or names none and supplies all three
itself. Mixing the two is refused: the member is disabled and `list_council`
reports the reason. Per-member `headers`, `timeout`, `temperature`, `max_tokens`
and `label` are unaffected and still override.

Also fixed: the test suite auto-discovered a developer's real
`~/.config/model-council/config.json` instead of its own fixtures, so tests
passed on CI and failed on any machine that had one.

## 0.1.0

First release. Any number of models from any mix of OpenAI-compatible and
Anthropic-compatible endpoints, exposed as four tools: `ask`, `ask_all`,
`list_council`, `probe_models`.
