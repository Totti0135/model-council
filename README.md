# Model Council

<!-- mcp-name: io.github.Totti0135/model-council -->

English | [简体中文](README.zh-CN.md)

An MCP server that seats other LLMs at your table. Your assistant asks them,
reads their answers as tool results, relays those answers back and forth for
critique, and gives you one merged conclusion — inside a single normal
conversation, with no copy-paste.

Your assistant chairs the council. Any number of members, from any mix of
OpenAI-compatible and Anthropic-compatible endpoints — a hosted API, a
self-run gateway, a local server, or several of each.

## Tools

| Tool | What it does |
|------|--------------|
| `ask(model, prompt, materials?)` | Ask one member by id |
| `ask_all(prompt, models?, rounds?, guests?, materials?)` | Ask everyone (or a named subset) the same prompt in parallel, answers side by side. `rounds=2` turns it into a discussion; `guests` seats answers you already have; `materials` hands them a file to read |
| `revise(prompt, answers, round?, materials?)` | Run one more round yourself: show the members everything said last round — including answers only you can produce — and get them back revised |
| `revision_prompt(prompt, answers, seat, materials?)` | The prompt to hand your own seat for the next round, word-for-word what the members got. No network calls |
| `list_council()` | The roster: ids, endpoints, weights, what each member can be shown, the route each takes out, call budget, and whether it is ready. No network calls |
| `probe_models(model?)` | Ask a provider's `/models` route what ids it really exposes |

Members are stateless and cannot see your conversation, so the chair passes
everything they need in each call. That is exactly what makes cross-review work:
it puts one member's answer inside another's `prompt`.

### Giving the council something to read

A question is usually about something — a spec, a log, a diff, a screenshot. The
obvious way to include it is to paste it into `prompt`, and that is the expensive
one, in the place nobody watches: `prompt` is an argument the chair *writes*, so
a long document costs a full copy of itself in generated tokens on every call,
and what reaches the members is whatever the chair managed to reproduce. For
forty pages that is not reliably the document. A council reviewing a paraphrase
is not reviewing the thing, and nothing in the transcript would say so.

`materials` names it instead:

```
ask_all(
  prompt="Where would this break under load?",
  materials=[
    {"label": "The design", "path": "/abs/path/design.md"},
    {"label": "Latency graph", "path": "/abs/path/p99.png"},
    {"label": "My summary", "text": "…something with no file behind it…"},
  ],
  rounds=2,
)
```

The server reads each one and puts it ahead of the question, in the same place
for every member and every round. The position is not presentation:

- **Identical bytes.** Every member argues with the same copy, which is what
  makes their disagreement about the document rather than about which version
  each was given.
- **A prefix that can be cached.** Everything up to the end of the material is
  the same in every call of a discussion. The Anthropic format is told so
  explicitly, with one `cache_control` breakpoint at that boundary; endpoints
  that cache by prefix on their own get the shape they need either way. Set
  `"cache": false` on a member whose gateway rejects the field.
- **The chair pays once.** A path costs the chair a line, not a copy of the
  document, and `ask_all` carries the material into round 2 and 3 by itself.

**Images are the case that matters most.** Without them the chair has to describe
the screenshot in prose — and then every member reads the *same* description, so
anything the chair misread is misread by the whole council at once, and
cross-review cannot recover it. It is the one input where passing it badly
quietly removes the independence the council is for. Png, jpeg, gif and webp go
to every member whose model can see; a member that cannot should be configured
`"vision": false`, and it then sits out the calls that carry an image rather than
answering about text alone as though it had seen the picture. The transcript
names who sat out.

**How to tell which members those are:** a member that cannot see rarely says so.
One Anthropic-compatible gateway tested while building this accepted the image
blocks, discarded them, and the model then named a colour — not "I was sent no
image", which the prompt had explicitly asked for in that case. Text material
through the same endpoint arrived intact, so nothing about the call looked wrong.
Show a member one unambiguous picture and ask what is in it; a confidently wrong
answer is the symptom, and `"vision": false` is the fix.

Two limits worth knowing. A material that cannot be read **stops the call** — a
council asked about a document it never received will answer anyway, fluently,
and read exactly like one that had. And `revision_prompt` *names* your files
rather than pasting them back, at the position the members were given them, so
open them for your own seat before you hand it the rest.

Whether this server will read a path at all depends on how it was started; over
HTTP it will not, unless its operator [named a directory](#material-over-http).
`list_council` says which, in a line above the table.

### Rounds

`ask_all(prompt, rounds=2)` runs that cross-review for you. Round 1 is the usual
parallel ask. Round 2 goes back to each member carrying the question plus every
answer from round 1 — its own and the others', verbatim — and asks it to revise:
take what is right, correct what is not, and say where it still disagrees and
why. The transcript comes back round by round, so you can see who moved and who
held their ground.

Carrying the previous round back is the whole mechanism. Members remember
nothing between calls, so without it a second round is just the same question
asked twice. Up to 3 rounds; each one costs another call per member and a longer
prompt than the last, so 1 is right for a survey of opinion and 2 for a question
where the disagreement is the interesting part.

**`rounds` is chosen before anything has been asked**, which is the one thing
wrong with it: you commit to a second round without having read the first, and a
council that turns out to agree costs exactly what one still arguing would. Ask
with `rounds=1` when you would rather look first, then buy the next round with
[`revise`](#a-subagent-as-a-full-member) — it runs exactly one more, as many
times as you judge it worth, and has no ceiling of its own. Reading before you
buy is usually the cheaper side: another round is a full call per member, while
`revise` costs you only the answers written back into it. The 3 on `ask_all` is a
ceiling on what one call will spend, not on what the discussion can have, and the
transcript says so when you reach it.

Members can also carry different [weights](#weights), for the common case where
the council is not a council of equals.

### Seating an answer you already have

Your assistant is not only the chair — it can answer too, and in Claude Code or
Codex it can spawn a subagent to answer as well. Those answers used to sit
*beside* the council's, compared by hand at the end. `guests` puts them *in* it:

```
ask_all(
  prompt="What are the traps in this plan?",
  guests=[{"label": "Subagent", "text": "<what your subagent answered>"}],
  rounds=2,
)
```

Round 1 prints it beside the members. From round 2 every member is handed the
text verbatim and asked to argue with it:

```
--- ANSWER C (does not revise between rounds) ---
the migration has no rollback path
```

That is the whole difference. Without it the models never learn your subagent had
an opinion, and you are left doing the comparison yourself — which is the work
the rounds mechanism already does better, because it lets the models answer each
other rather than answering into a void.

Notes worth having:

- **Pass the text verbatim, not a summary.** The members are shown exactly what
  you pass, and a summary is not the thing you wanted critiqued.
- **A guest speaks once.** There is nobody to ask it for a revision, so it does
  not reappear after round 1. The transcript says so, because a seat that
  vanishes otherwise reads as a position abandoned.
- **A guest counts as a voice.** One member plus one guest is enough for a real
  second round — your subagent against one model is still a discussion.
- **`weight` works on guests too**, on the same scale as the members'.
- Guests are per call. Nothing is configured, and nothing persists.

### A subagent as a full member

A guest speaks once. To let a voice you produce yourself *keep up* with the
members — answer, read the others, revise, round after round, exactly as they do
— you have to drive the rounds, because this server cannot spawn your subagent.
`revise` runs one round on demand:

```
ask_all(prompt)                                  → members answer round 1
  ...you spawn your subagent on the same prompt   → its round-1 answer

revise(prompt, round=1, answers=[
  {"model": "glm", "text": "<glm's round-1 answer>"},
  {"model": "sol", "text": "<sol's round-1 answer>"},
  {"label": "Subagent", "text": "<subagent's round-1 answer>"},
])                                               → members come back revised
  ...you re-run your subagent on the same material

revise(prompt, round=2, answers=[...round 2...]) → and so on
```

Naming the member with `model` is what makes it a revision: that member is handed
its own previous answer back as its own, and asked to move from it rather than
answer fresh. An entry with `label` instead is a voice from outside the roster.

**The members cannot tell the difference.** An outsider passed to `revise` is
presented exactly as another member is — a bare letter, `--- ROUND-1 ANSWER C ---`
— because it will answer again next round, and it is the same reason an `ask_all`
guest *is* flagged as finished. Describing a voice as done when it is about to
speak again misrepresents the discussion to the models doing the arguing.

**Prompt your subagent with `revision_prompt`, not with the original question.**
This is the one way to get the loop wrong, and it fails silently: a subagent
handed the question again simply reproduces its previous answer, the transcript
still looks like a discussion, and nothing marks the round where that seat
stopped taking part.

```
revision_prompt(prompt, answers=[...same as revise...], seat="Subagent", round=1)
```

It returns exactly what the members were given — its own previous answer marked
as its own, everyone else's, and the same closing instruction, including the line
telling it not to abandon a position it still believes just because it is
outnumbered. That sentence is most of what keeps a council from collapsing into
agreement, and a seat prompted without it is not being asked the same question as
the others. The call is local and makes no network requests, so run it alongside
`revise` rather than after it.

`revise` is unbounded — the 3-round ceiling on `ask_all` exists because that call
spends its own budget, while here every round is one you chose to pay for.

| | `ask_all(guests=…)` | `revise(answers=…)` |
|---|---|---|
| Calls | one | one per round |
| Your voice | speaks once | revises every round |
| Rounds driven by | the server | you |

### The members argue anonymously

Inside a revision round the other answers arrive as letters, never as names:

```
--- ROUND-1 ANSWER B ---
concurrent writes race
```

A model name is the same kind of signal as a weight, and the stronger of the two:
models hold firm priors about each other's makers. Withholding the number while
printing the brand would close the smaller channel and leave the larger one open.
What reaches a member is the argument, which is the thing we wanted weighed.

Letters come from position at the table, so they are stable: `B` is the same seat
to every member and in every round, which is what lets one of them say "B was
wrong about the index" and still be understood next round.

**You keep the names.** Every transcript that contains a revision round ends with
the key:

```
[the members saw each other as letters, not names: A = GLM-5.3, B = GPT-5.6-sol,
C = Subagent. ...]
```

You are the exception because you are the one who has to tell them apart — see
who moved, who held, and who was talking about whom.

One honest limit: this covers the labels the server writes, not a name a model
puts inside its own answer. That prose is carried verbatim into the next round,
because the alternative is editing the text the others are meant to be
critiquing. Anonymity here removes the standing signal, not every mention.

### Retries

A call that fails transiently is retried before it is reported: HTTP 429 and
5xx, and dropped or timed-out connections. Backoff is exponential from 1s with
jitter, and a `Retry-After` header wins over that curve — unless it asks for
longer than 30s, in which case the call stops and says so rather than sitting on
it. Failures that will not change on a second look — 401, 404, a malformed
response body — are reported immediately; retrying them only spends the same
quota to be told the same thing. Defaults to 2 retries; set `retries: 0` for the
old single-shot behaviour.

A member that exhausts its attempts returns its error as that member's answer,
so the rest of the council still answers.

## Install

The server is listed on the official [MCP Registry](https://registry.modelcontextprotocol.io)
as `io.github.Totti0135/model-council`, so a client that browses the registry can
find and add it there. To wire it up by hand instead, read on.

It runs from PyPI with no clone and no virtualenv. You need
[uv](https://docs.astral.sh/uv/):

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

### Claude Desktop

Easiest is the desktop extension. Download `model-council-<version>.mcpb` from
the [latest release](https://github.com/Totti0135/model-council/releases/latest)
and drag it onto Settings → Extensions. The app asks for the endpoints and keys
in a form and keeps the keys in your OS keychain, so no file on disk holds them.
The form seats two models; for a larger council, point its "Config file" field
at a JSON config (see below).

To wire it up by hand instead, edit `claude_desktop_config.json`
(Settings → Developer → Edit Config), add the block below, then **fully quit and
reopen** the app — Cmd-Q, not just closing the window. You will know it worked
when the tools menu lists `model-council`.

```json
{
  "mcpServers": {
    "model-council": {
      "command": "uvx",
      "args": ["model-council-mcp"],
      "env": {
        "COUNCIL_MODELS": "gpt5,glm",
        "GPT5_BASE_URL": "https://your-openai-compatible-host/v1",
        "GPT5_API_KEY": "sk-xxxxxxxx",
        "GPT5_MODEL": "gpt-5",
        "GLM_BASE_URL": "https://open.bigmodel.cn/api/anthropic",
        "GLM_API_KEY": "xxxxxxxx",
        "GLM_MODEL": "glm-4.6",
        "GLM_FORMAT": "anthropic"
      }
    }
  }
}
```

### Claude Code

```bash
claude mcp add model-council -e GPT5_BASE_URL=... -e GPT5_API_KEY=... -- uvx model-council-mcp
```

### Other MCP clients

Anything that launches a stdio server works: run `uvx model-council-mcp` and
pass the same environment variables.

## Serving a team over HTTP

Everything above runs one copy of the server per person, launched by their own
MCP client, configured with their own keys. `--http` is the other shape: one
deployment holds one set of keys and answers a whole team, who configure a URL
and no secret at all.

```bash
model-council-mcp --http --host 0.0.0.0 --allow 10.20.0.0/16
```

Colleagues then add it as a remote server, with nothing sensitive in the config:

```bash
claude mcp add --transport http model-council http://council.internal:8000/mcp
```

```jsonc
// Claude Desktop and other clients
{ "mcpServers": { "model-council": { "type": "http", "url": "http://council.internal:8000/mcp" } } }
```

`deploy/` has a [Dockerfile](deploy/Dockerfile), a
[compose file](deploy/compose.yaml) and a [systemd unit](deploy/model-council.service).

### Who may call

Over stdio the operating system answers this: whoever launched the process
already had the keys. HTTP removes that guarantee — one port now stands in front
of shared provider quota — so the server **refuses to start** on a non-loopback
address until `--allow` says who may reach it. Loopback needs no flag, and is
always admitted.

| Flag | |
|------|---|
| `--allow` | CIDRs, bare addresses, or the names `private` (RFC1918), `loopback`, `any` |
| `--trust-proxy` | reverse proxies whose `X-Forwarded-For` may be believed |
| `--allow-origin` | permit a browser `Origin`; repeatable |
| `--materials-root` | the one directory `materials` paths may name; without it, HTTP refuses paths |

`--trust-proxy` is the one worth reading twice. Without it `X-Forwarded-For` is
ignored entirely and the peer address decides — behind nginx that is nginx, so
the allowlist matches everyone or no one. With it, the client is the rightmost
hop in the chain that is *not* a trusted proxy, which is what stops a caller
from writing `X-Forwarded-For: 10.0.0.1` and walking straight through.

Requests carrying an `Origin` header are refused by default. MCP clients are not
browsers and do not send one; a web page always does. An allowlist admits every
machine on the office network, and each of those runs a browser that will issue
requests on behalf of whatever page it has open — `Origin` is what tells the two
apart.

Every flag has an environment variable (`COUNCIL_ALLOW`, `COUNCIL_TRUST_PROXY`,
`COUNCIL_HTTP_HOST`, …); `--help` lists them.

<a name="material-over-http"></a>

### Material over HTTP

Over stdio, `materials` reads any file the process can, and needs no flag: the
caller launched this process, so it already had that access — the same reasoning
that lets a loopback bind skip `--allow`. Over HTTP the reasoning is gone. The
caller is whoever reached the port, a path would make one shared council into a
way to read its host's files, and what it read would leave for an external
provider. So an HTTP deployment **refuses paths outright**, and callers pass the
contents as `text`.

`--materials-root /srv/council/material` opens exactly one directory, resolved
before it is compared, so a symlink is judged by where it lands.

### What this does not do

The allowlist is a network boundary, not an identity. Anyone inside it calls
without a credential, so usage cannot be attributed to a person, rate-limited
per person, or revoked for one person. That is a deliberate trade — it is what
makes the client config a bare URL — but it means the network has to be a
boundary you actually trust, and it does not survive contact with a VPN that
admits contractors, or a CI runner on the same subnet.

If you need per-person attribution or quota, put an LLM gateway (LiteLLM,
one-api, or whatever your organisation already runs) behind this server and give
each caller their own virtual key there, or run this behind a reverse proxy that
does SSO.

One more thing worth knowing before you announce the URL: this service forwards
whatever text it is given to an external provider. A shared endpoint with no
credential is a data-egress path for everyone who can reach it.

### Behind a reverse proxy

`ask_all` with `rounds=2` is a long request — several models, several attempts
each, at up to `COUNCIL_TIMEOUT` (180s) per call. Default proxy timeouts will
cut it off well before the server is finished:

```nginx
location /mcp {
    proxy_pass http://127.0.0.1:8000;
    proxy_http_version 1.1;
    proxy_buffering off;          # the transport streams; buffering defeats it
    proxy_read_timeout 900s;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
}
```

Then start the server with `--trust-proxy` set to the proxy's address, or the
allowlist will only ever see the proxy.

## Configuring the council

Two layers, so several models can share one endpoint without repeating its
credentials:

- **provider** — an endpoint: `base_url` + `api_key` + which wire format it speaks
- **member** — one model on some provider, addressed by a short id

Configuration comes from whichever source is most explicit: the file
`COUNCIL_CONFIG` points at, else the roster `COUNCIL_MODELS` names, else a config
file at `~/.config/model-council/config.json`, else the built-in default roster.
Explicit beats discovered on purpose — a config file you left lying around must
not silently override settings a client just handed the server.
`list_council()` always reports which source won.

### Environment variables

`COUNCIL_MODELS` lists the ids; each id gets variables named after it, uppercased
with non-alphanumeric characters turned into underscores (`my-model` →
`MY_MODEL_BASE_URL`).

```bash
COUNCIL_MODELS=gpt5,glm
GPT5_BASE_URL=https://your-openai-compatible-host/v1
GPT5_API_KEY=sk-xxxxxxxx
GPT5_MODEL=gpt-5
GLM_BASE_URL=https://open.bigmodel.cn/api/anthropic
GLM_API_KEY=xxxxxxxx
GLM_MODEL=glm-4.6
GLM_FORMAT=anthropic
```

Per member: `_BASE_URL`, `_API_KEY`, `_MODEL`, `_FORMAT`, `_LABEL`, `_WEIGHT`,
`_MAX_TOKENS`, `_TEMPERATURE`, `_TIMEOUT`, `_RETRIES`, `_RETRY_BACKOFF`,
`_HEADERS` (a JSON object), `_PROXY`, `_VISION`, `_CACHE`, `_ENABLED`. Globally:
`COUNCIL_TIMEOUT`, `COUNCIL_RETRIES`, `COUNCIL_RETRY_BACKOFF`, `COUNCIL_PROXY`,
`COUNCIL_CONFIG`, `COUNCIL_ENV_FILE`, `COUNCIL_MATERIALS_ROOT`.

Omit `COUNCIL_MODELS` and the roster defaults to `chatgpt,glm`, reading
`CHATGPT_*` and `GLM_*`.

### A config file

Better once you have more than a handful of members, or when several share an
endpoint. Set `COUNCIL_CONFIG=/path/to/config.json`, or drop the file at
`~/.config/model-council/config.json` where the server finds it on its own.

```json
{
  "providers": {
    "my-relay": {
      "base_url": "https://your-openai-compatible-host/v1",
      "api_key": "${MY_RELAY_KEY}",
      "format": "openai"
    },
    "zhipu": {
      "base_url": "https://open.bigmodel.cn/api/anthropic",
      "api_key": "${GLM_KEY}",
      "format": "anthropic"
    }
  },
  "members": [
    { "id": "gpt5",  "provider": "my-relay", "model": "gpt-5", "label": "GPT-5",
      "weight": 2 },
    { "id": "codex", "provider": "my-relay", "model": "gpt-5-codex", "temperature": 0.2 },
    { "id": "glm",   "provider": "zhipu",    "model": "glm-4.6" },
    { "id": "kimi",  "base_url": "https://api.moonshot.cn/v1",
      "api_key": "${KIMI_KEY}", "model": "kimi-k2" }
  ]
}
```

`${ENV_VAR}` is expanded from the environment, so the file carries no secrets and
can be shared or committed. See [examples/config.json](examples/config.json) for a
fully annotated version.

A member gets its connection one of two ways, never a mix of both: name a
`provider` and take that endpoint whole, or omit `provider` and supply
`base_url` + `api_key` + `format` yourself (as `kimi` does above). Naming a
provider *and* overriding one of those three is refused — that member is
disabled and `list_council` says why. The reason is that a partial override
would pair one endpoint's credentials with another endpoint's URL, quietly
sending your key to a host it was never issued for. Per-member `headers`,
`timeout`, `temperature`, `max_tokens` and `label` are not part of that
identity and stay overridable.

### Fields

The first three travel together as one unit — see the rule above.

| Field | Applies to | Notes |
|-------|-----------|-------|
| `base_url` | provider, or a member with no provider | Root the route hangs off — `/chat/completions` for openai, `/v1/messages` for anthropic. Usually ends in `/v1` for OpenAI-compatible hosts |
| `api_key` | provider, or a member with no provider | |
| `format` | provider, or a member with no provider | `openai` (default) or `anthropic` |
| `model` | member | The model id sent to the endpoint |
| `label` | member | Display name in answers; defaults to the id |
| `weight` | member | How far this member's opinion carries. Default 1, max 10, `0` for advisory only. See [Weights](#weights) |
| `max_tokens` | member | Anthropic format only, where it is required. Default 8192 |
| `temperature` | member | Sent only when set |
| `headers` | provider, member | Extra HTTP headers |
| `timeout` | provider, member | Seconds, per attempt. Default 180 |
| `retries` | provider, member | Extra attempts a transient failure gets. Default 2, max 5, `0` to disable |
| `retry_backoff` | provider, member | Seconds before the first retry, doubling from there. Default 1 |
| `proxy` | top level, provider, member | The route out. Omit to follow `HTTP_PROXY`/`HTTPS_PROXY`; `false` to connect directly; a URL to use that proxy; `"env"` to rejoin the environment's. See [Proxies](#proxies) |
| `vision` | member | `false` for a model that cannot be shown an image. It then sits out calls carrying one, rather than answering about the text alone. Default `true` |
| `cache` | provider, member | `false` stops the `cache_control` breakpoint being sent with material. Anthropic format only; turn it off for a gateway that rejects the field. Default `true` |
| `enabled` | member | `false` parks a member without deleting its config |

`timeout`, `retries`, `retry_backoff` and `proxy` can also be set at the top level
of the config file, as the default every member inherits.

### Weights

A council is rarely made of equals. `weight` says how far a member's opinion
carries — everyone is `1` until you say otherwise, and only the ratios mean
anything, so `2` and `1` is the same council as `10` and `5`.

```json
{ "id": "gpt5", "provider": "my-relay", "model": "gpt-5", "weight": 2 }
```

It changes nothing about the call. When the weights differ, `ask_all` labels each
answer with its weight and ends the transcript with the ranking:

```
===== GPT-5 (gpt-5) · weight 2 =====
...
===== Local (qwen3-8b) · weight 0.5 =====
...

[WEIGHTS — GPT-5 2, GLM 1, Local 0.5]
These are this council's standing priors on its members, not votes. ...
```

Weight belongs to the seat, not the endpoint, so a provider cannot set it: two
members on one relay may be a frontier model and a small fast one. `0` means
advisory — the member answers and is read, but its agreement counts for nothing.
Anything unusable (a negative, a word) falls back to `1` rather than corrupting
the ranking silently; `list_council` prints the effective value.

**The members are never told each other's weights.** A model informed that it is
outranked stops arguing and starts agreeing, which costs exactly the independent
dissent a council is assembled to produce — so round two carries the other
answers and not their standing. The weights are for whoever reads the transcript,
and they are a prior, not a vote: they break ties and decide who carries the
burden of proof. A specific, checkable reason from the lowest weight still beats
a bare assertion from the highest.

### Proxies

Every member follows this machine's `HTTP_PROXY`/`HTTPS_PROXY`, which is the
right default until a council spans hosts that do not share a route — a public
API that only answers through the proxy, an internal gateway the proxy cannot
see. One setting cannot be right for both, so the route is chosen per seat.

`proxy` takes four values, on a member, on a provider, or at the top level of the
config file as the council's own default:

| Value | The route |
|-------|-----------|
| omitted | Follow the council's `proxy` if it sets one, else `HTTP_PROXY`/`HTTPS_PROXY` |
| `false`, or `"direct"` | Straight out, ignoring both |
| a URL | Through that proxy — `http://`, `https://`, `socks5://` or `socks5h://` |
| `"env"` | Back onto `HTTP_PROXY`/`HTTPS_PROXY`, for one seat of a council routed elsewhere |

Most specific wins: member, then provider, then the council, then the
environment. So a council that is mostly behind a proxy, with two seats that must
not be, says it once and then twice:

```json
{
  "proxy": "http://127.0.0.1:7890",
  "providers": {
    "internal": { "base_url": "https://gateway.internal.example/v1",
                  "api_key": "${INTERNAL_KEY}", "proxy": false }
  },
  "members": [
    { "id": "gpt5",  "provider": "my-relay", "model": "gpt-5" },
    { "id": "inhouse", "provider": "internal", "model": "some-internal-model" },
    { "id": "local", "base_url": "http://127.0.0.1:11434/v1", "api_key": "-",
      "model": "qwen3-8b", "proxy": false }
  ]
}
```

The same three moves through environment variables — `COUNCIL_PROXY` for the
council, `<ID>_PROXY` for one member:

```bash
COUNCIL_PROXY=http://127.0.0.1:7890
INHOUSE_PROXY=direct
LOCAL_PROXY=direct
```

**`list_council` prints the route it settled on**, as `env`, `direct`, or the
proxy URL, in a column that appears only when the members can differ:

```
network: HTTPS_PROXY=http://127.0.0.1:7890 in this server's environment — the members whose route is 'env' go through it

id       label    model      weight  format  sees      route                  endpoint                          tries     status
gpt5     GPT-5    gpt-5      1       openai  text+img  env                    https://your-host/v1              3 × 180s  ready
inhouse  Inhouse  internal   1       openai  text+img  direct                 https://gateway.internal/v1       3 × 180s  ready
kimi     Kimi     kimi-k2    1       openai  text+img  http://127.0.0.1:7890  https://api.moonshot.cn/v1        3 × 180s  ready
```

A password inside a proxy URL is masked wherever it is printed — the table, the
warnings, the text of a connection error.

**A proxy that cannot be used is caught when the roster is read**, not on the
first call. `127.0.0.1:7890` with the scheme left off is read as
`http://127.0.0.1:7890` and says so in the warnings; a scheme nothing can dial
parks that member with the reason next to it in `list_council`, rather than
letting it fail every call with a `ValueError` raised from inside a request. The
member is parked rather than quietly sent another way: a proxy is named because
someone wants the traffic to go that way.

`socks5://` needs one package httpx does not install by default. Install the
extra — `pip install 'model-council-mcp[socks]'`, or
`uvx --from 'model-council-mcp[socks]' model-council-mcp` — or that member is
parked with a note saying exactly this.

### Wire format notes

- **`format` is not inferred from the URL.** Pointing `base_url` at an
  Anthropic-style endpoint without also setting `format: "anthropic"` leaves the
  member on the OpenAI format, and every call fails. This is the single most
  common misconfiguration.
- **Anthropic endpoints:** the server posts to `{base_url}/v1/messages`, so
  `base_url` should not already include the `/v1`.
- **OpenAI-compatible endpoints:** the server uses `/chat/completions`, never
  `/responses`. Some gateways expose both, but `/responses` may inject a
  provider-chosen system persona, which is wrong for a general-purpose advisor.
- **A system proxy is followed by default.** If a member sits on a network your
  proxy cannot reach — an internal gateway, typically — it fails with a bare
  `ConnectError` that never mentions a proxy. Give that member or provider
  `"proxy": false` and it connects directly, while everyone else keeps using the
  proxy; see [Proxies](#proxies). Connection errors name the route the member
  was on, so the three policies do not all fail the same way.
- **Model ids move fast.** Run `probe_models` to see what an endpoint actually
  offers today.

## Using it

Things worth typing to the chair:

- *"Answer this yourself, then `ask_all` and give me a table of where you all agree and disagree."*
- *"Ask gpt5 and glm this, then critique both answers and tell me which is more correct and why."*
- *"Run two rounds of `ask_all` on this, then tell me who changed their mind and what actually settled it."*
- *"Ask only glm — I want a second opinion on this one file."*

## Local development

```bash
uv sync
```

Copy `.env.example` to `.env`, fill in real values, then:

```bash
uv run python tests/test_smoke.py
```

The smoke test checks both configuration paths offline; with a usable `.env` it
finishes with a live round-trip. To point a client at your working copy, use the
`model-council-mcp` script inside your environment instead of `uvx`.

## Troubleshooting

- **Server doesn't appear** — check the client's MCP logs (Claude Desktop:
  `~/Library/Logs/Claude/mcp*.log`). The server writes configuration warnings to
  stderr at startup.
- **A tool answers `[... is not configured]`** — that member is missing
  `base_url`, `api_key`, or `model`. Run `list_council` for a per-member breakdown.
- **HTTP 401** — wrong key, or a key the provider has disabled.
- **HTTP 404** — wrong `base_url`, or the wrong `format` for that endpoint.
- **The model id is rejected** — run `probe_models`.
- **A member says `gave up after N attempts`** — it failed transiently every
  time. The error text is the last one the endpoint gave. `list_council` shows
  each member's budget as `attempts × timeout`.
- **A call takes far longer than the timeout** — retries multiply it: three
  attempts at 180s each is a worst case of ~9 minutes plus backoff. Lower
  `timeout`, or `retries`, for a member you would rather have fail fast.
- **One member fails with a bare `ConnectError` while the rest are fine** — it is
  usually the route, not the endpoint. The error names which route that member
  was on; `list_council`'s `route` column shows all of them at once, and
  [Proxies](#proxies) is how to change one.

## License

MIT
