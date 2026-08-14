# Changelog

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
