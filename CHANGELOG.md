# Changelog

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
