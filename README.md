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
| `ask(model, prompt)` | Ask one member by id |
| `ask_all(prompt, models?)` | Ask everyone (or a named subset) the same prompt in parallel, answers side by side |
| `list_council()` | The roster: ids, endpoints, and whether each member is ready. No network calls |
| `probe_models(model?)` | Ask a provider's `/models` route what ids it really exposes |

Members are stateless and cannot see your conversation, so the chair passes
everything they need in each call. That is exactly what makes cross-review work:
it puts one member's answer inside another's `prompt`.

## Install

The server runs from PyPI with no clone and no virtualenv. You need
[uv](https://docs.astral.sh/uv/):

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

### Claude Desktop

Edit `claude_desktop_config.json` (Settings → Developer → Edit Config), add the
block below, then **fully quit and reopen** the app — Cmd-Q, not just closing
the window. You will know it worked when the tools menu lists `model-council`.

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

## Configuring the council

Two layers, so several models can share one endpoint without repeating its
credentials:

- **provider** — an endpoint: `base_url` + `api_key` + which wire format it speaks
- **member** — one model on some provider, addressed by a short id

Configuration comes from a JSON file if one is found, otherwise from environment
variables. `list_council()` always reports which source won.

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

Per member: `_BASE_URL`, `_API_KEY`, `_MODEL`, `_FORMAT`, `_LABEL`, `_MAX_TOKENS`,
`_TEMPERATURE`, `_TIMEOUT`, `_HEADERS` (a JSON object), `_PROXY`, `_ENABLED`.
Globally: `COUNCIL_TIMEOUT`, `COUNCIL_CONFIG`, `COUNCIL_ENV_FILE`.

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
    { "id": "gpt5",  "provider": "my-relay", "model": "gpt-5", "label": "GPT-5" },
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
| `max_tokens` | member | Anthropic format only, where it is required. Default 8192 |
| `temperature` | member | Sent only when set |
| `headers` | provider, member | Extra HTTP headers |
| `timeout` | provider, member | Seconds. Default 180 |
| `proxy` | provider, member | Omit to follow `HTTP_PROXY`/`HTTPS_PROXY`; `false` to connect directly; a URL to use that proxy |
| `enabled` | member | `false` parks a member without deleting its config |

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
  proxy. The error message says so too when a proxy is in play.
- **Model ids move fast.** Run `probe_models` to see what an endpoint actually
  offers today.

## Using it

Things worth typing to the chair:

- *"Answer this yourself, then `ask_all` and give me a table of where you all agree and disagree."*
- *"Ask gpt5 and glm this, then critique both answers and tell me which is more correct and why."*
- *"Round 1: `ask_all`. Round 2: show each member the others' answers and ask it to revise. Then give me the merged answer."*
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

## License

MIT
