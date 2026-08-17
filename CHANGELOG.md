# Changelog

## 0.6.0

**A seat your own model can take.** A member with `format: "sampling"` has no
endpoint and no key: the server sends `sampling/createMessage` back down the open
MCP session and the client answers with its own model. It joins rounds like any
other member, costs no quota, and needs one line of config:

```json
{ "id": "sub", "format": "sampling", "label": "Subagent" }
```

Its answers are labelled `(this client)` rather than with a model id, and any
transcript containing one ends with a note saying what it is: a fresh instance
carrying none of the conversation, but the same model that is reading the
transcript and writing the conclusion. A second look, not a second opinion —
counting its agreement as corroboration is the one mistake the seat makes easy,
and the note exists to spend a sentence preventing it. A seat that only ever
errored draws no such note, because then there is no answer of ours to caveat.

Two things it cannot do, both said plainly rather than discovered: a client that
never offered the sampling capability gets a message explaining exactly that
while the rest of the council answers normally, and the seat cannot work over
`--http` at all — sampling is a request travelling back to the client and this
server is stateless, so it warns at startup instead of failing per call.

**This rides a deprecation window on purpose.** MCP deprecated Sampling in the
`2026-07-28` spec (SEP-2577) with a minimum twelve-month window, suggesting new
work "integrate directly with LLM provider APIs instead" — which is what every
other member here already does. It is kept because it is the only way to seat the
client's own model without a second key, and because the replacement pattern
(MRTR) resolves its request once before the tool body runs, which a multi-round
council cannot express. If Sampling is removed, this seat stops working and
nothing else does.

## 0.5.0

**`--http` serves a team from one deployment.** Until now the server only spoke
stdio, which means one copy per person, launched by their own MCP client, holding
their own keys. `model-council-mcp --http` listens over Streamable HTTP instead,
so one deployment holds one set of keys and answers everyone — colleagues
configure a URL and no secret at all:

```bash
model-council-mcp --http --host 0.0.0.0 --allow 10.20.0.0/16
claude mcp add --transport http model-council http://council.internal:8000/mcp
```

Nothing about stdio changed; it is still the default and still what `uvx`
launches. `deploy/` adds a Dockerfile, a compose file and a systemd unit.

**The HTTP server will not start without being told who may call it.** Over
stdio the operating system settled that question — whoever launched the process
already had the keys. A port does not, and this one stands in front of shared
provider quota, so binding a non-loopback address without `--allow` is a startup
error rather than a default. `--allow` takes CIDRs, bare addresses, or the names
`private`, `loopback` and `any`; loopback is always admitted.

`--trust-proxy` names the reverse proxies whose `X-Forwarded-For` may be
believed. Without it the header is ignored and the peer address decides, which
behind nginx is nginx; with it the client is the rightmost hop that is not a
trusted proxy, so a caller cannot write themselves onto the allowlist. uvicorn's
own proxy-header handling is turned off, so exactly one mechanism decides this.

Requests carrying an `Origin` header are refused unless `--allow-origin` names
it. MCP clients are not browsers and send none; a page always does. An allowlist
admits every machine on a network, and each one runs a browser that will issue
requests for whatever page it has open.

The allowlist is a network boundary and not an identity: callers inside it are
anonymous, so usage cannot be attributed, throttled or revoked per person. The
README says so plainly, and says what to reach for when that is not enough.

**`GET /healthz`** reports liveness and how many members are configured — counts
only, never the roster. It answers 503 while no member is configured, so a
deploy that came up with an unreadable config file fails its health check
instead of looking fine and erroring on every tool call.

**Members can carry a `weight`.** A council is rarely made of equals, and until
now the transcript gave no way to tell a frontier model's answer from a small
local one's. `weight` says how far a member's opinion carries — default 1, max
10, `0` for a seat that is read but counts for nothing — set per member via
`<ID>_WEIGHT` or a `weight` field, and shown in `list_council`.

It changes nothing about the call. When the weights differ, `ask_all` labels each
answer with its weight and closes the transcript with the ranking; when they are
all equal it says nothing, because a weight only means anything next to another
one. Weight is a member field and not a provider field: two members on one relay
can be a frontier model and a small fast one.

The weights go to the caller and never to the members. A model told it is
outranked stops arguing and starts agreeing, which costs exactly the independent
dissent a council is assembled to produce — so round two still carries the other
answers and not their standing. They are also stated to be a prior rather than a
vote: they break ties and place the burden of proof, and a checkable reason from
the lowest weight still beats a bare assertion from the highest.

## 0.4.1

**0.4.0 told clients it was 0.3.0.** The version lives in four files and the
0.4.0 release bumped three of them: `__init__.__version__` was left behind, and
that is the one passed to `MCPServer(...)`, so the package announced the wrong
version in the MCP handshake. Cosmetic — every tool in 0.4.0 behaves as
documented — but not fixable in place, since PyPI does not allow a version to be
re-uploaded.

The smoke test now checks `__init__` against `pyproject.toml` alongside
`server.json` and `manifest.json`. It already cross-checked the other three,
which is exactly why this was the one that drifted.

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
