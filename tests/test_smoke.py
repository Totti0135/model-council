#!/usr/bin/env python3
"""Smoke test — no pytest required, run it directly:

    python tests/test_smoke.py

Verifies the tool surface, both configuration sources, provider inheritance and
${ENV} expansion, retry behaviour, multi-round discussion, and graceful
degradation when a member is unconfigured. If a usable .env is present it
finishes with a live round-trip through ask_all.
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path

import httpx

from model_council import config as cfg


@contextmanager
def env(**overrides: str):
    """Run with exactly the given COUNCIL_/model vars set, nothing inherited.

    Also points the default config path somewhere that cannot exist: otherwise a
    developer's own ~/.config/model-council/config.json is auto-discovered and
    silently replaces whatever the test meant to set up.
    """
    saved = dict(os.environ)
    saved_default = cfg.DEFAULT_CONFIG_PATH
    for k in list(os.environ):
        # `_PROXY` also clears HTTP_PROXY/HTTPS_PROXY, which is the point: a
        # developer behind a proxy would otherwise get a different route — and a
        # different `list_council` table — from everyone else running this.
        if k.startswith("COUNCIL_") or k.endswith(
            ("_BASE_URL", "_API_KEY", "_MODEL", "_FORMAT", "_HEADERS", "_ENABLED",
             "_WEIGHT", "_PROXY")
        ):
            del os.environ[k]
    os.environ["COUNCIL_ENV_FILE"] = "/nonexistent"
    os.environ.update(overrides)
    cfg.DEFAULT_CONFIG_PATH = Path("/nonexistent/model-council/config.json")
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(saved)
        cfg.DEFAULT_CONFIG_PATH = saved_default


def test_default_roster() -> None:
    with env():
        c = cfg.load_council()
    assert sorted(c.members) == ["chatgpt", "glm"], c.members
    assert c.members["glm"].base_url == "https://open.bigmodel.cn/api/paas/v4"
    assert not c.members["chatgpt"].configured
    assert c.members["chatgpt"].missing == ["base_url", "api_key"], c.members["chatgpt"].missing
    print("ok  default roster (legacy CHATGPT_*/GLM_* prefixes still work)")


def test_env_roster() -> None:
    with env(
        COUNCIL_MODELS="gpt5, glm, kimi",
        GPT5_BASE_URL="https://relay-a/v1/",
        GPT5_API_KEY="sk-a",
        GPT5_MODEL="gpt-5.6-sol",
        GPT5_LABEL="GPT-5.6",
        GLM_BASE_URL="https://open.bigmodel.cn/api/anthropic",
        GLM_API_KEY="k",
        GLM_MODEL="glm-5.2",
        GLM_FORMAT="anthropic",
        KIMI_ENABLED="false",
    ):
        c = cfg.load_council()
    assert c.ids == ["gpt5", "glm"], c.ids           # kimi disabled, and not in ids
    assert c.members["gpt5"].label == "GPT-5.6"
    assert c.members["gpt5"].base_url == "https://relay-a/v1"   # trailing slash trimmed
    assert c.members["glm"].format == "anthropic"
    assert c.get("kimi") is None                      # disabled members are unreachable
    print("ok  env roster (COUNCIL_MODELS, labels, per-member enable)")


def test_file_config() -> None:
    doc = {
        "providers": {
            "relay-a": {"base_url": "https://relay-a/v1", "api_key": "${RELAY_A_KEY}",
                        "format": "openai", "headers": {"X-Trace": "council"}},
            "zhipu": {"base_url": "https://open.bigmodel.cn/api/anthropic",
                      "api_key": "${GLM_KEY}", "format": "anthropic"},
        },
        "members": [
            {"id": "gpt5", "provider": "relay-a", "model": "gpt-5.6-sol", "label": "GPT-5.6"},
            {"id": "codex", "provider": "relay-a", "model": "gpt-5-codex", "temperature": 0.2},
            {"id": "glm", "provider": "zhipu", "model": "glm-5.2", "max_tokens": 4096},
            {"id": "kimi", "base_url": "https://api.moonshot.cn/v1",
             "api_key": "${KIMI_KEY}", "model": "kimi-k2"},
            {"id": "bad", "provider": "nope", "model": "x", "typo_field": 1},
        ],
    }
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "config.json"
        path.write_text(json.dumps(doc), encoding="utf-8")
        with env(COUNCIL_CONFIG=str(path), RELAY_A_KEY="sk-a", GLM_KEY="sk-g", KIMI_KEY="sk-k"):
            c = cfg.load_council()

    # two members share one provider's endpoint and key — declared once
    assert c.members["gpt5"].base_url == c.members["codex"].base_url == "https://relay-a/v1"
    assert c.members["gpt5"].api_key == "sk-a"                    # ${ENV} expanded
    assert c.members["codex"].headers == {"X-Trace": "council"}   # inherited from provider
    assert c.members["codex"].temperature == 0.2                  # member-level override
    assert c.members["glm"].max_tokens == 4096
    assert c.members["kimi"].configured                           # inline connection, no provider
    assert c.members["kimi"].label == "kimi"                      # label defaults to id
    assert not c.members["bad"].configured
    assert any("unknown provider 'nope'" in w for w in c.warnings), c.warnings
    assert any("typo_field" in w for w in c.warnings), c.warnings
    print("ok  file config (provider reuse, ${ENV}, overrides, warnings)")


def test_connection_is_atomic() -> None:
    """A member may not take one provider's credentials to another endpoint.

    Merging a partial override would send relay-a's key to whatever host the
    member named, with nothing in the output to show it happened.
    """
    doc = {
        "providers": {
            "relay-a": {"base_url": "https://relay-a.example/v1",
                        "api_key": "SECRET-OF-RELAY-A", "format": "openai",
                        "headers": {"X-Trace": "council"}},
        },
        "members": [
            {"id": "ok", "provider": "relay-a", "model": "gpt-5"},
            # the dangerous shape: provider named, but the endpoint swapped out
            {"id": "moved", "provider": "relay-a", "model": "gpt-5",
             "base_url": "https://someone-elses-host.example/v1"},
            # benign per-member overrides stay allowed
            {"id": "tuned", "provider": "relay-a", "model": "gpt-5", "timeout": 30,
             "headers": {"X-Trace": "other"}, "temperature": 0.2},
            # a complete standalone connection is fine — nothing to mix
            {"id": "solo", "base_url": "https://solo.example/v1",
             "api_key": "SOLO-KEY", "model": "m"},
        ],
    }
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "config.json"
        path.write_text(json.dumps(doc), encoding="utf-8")
        with env(COUNCIL_CONFIG=str(path)):
            c = cfg.load_council()

    moved = c.members["moved"]
    assert moved.api_key != "SECRET-OF-RELAY-A", "relay-a's key leaked to another host"
    assert not moved.enabled and c.get("moved") is None, "the mixed member is still reachable"
    assert "mixes a provider" in moved.disabled_reason, moved.disabled_reason
    assert any("disabled" in w and "moved" in w for w in c.warnings), c.warnings

    assert c.members["ok"].api_key == "SECRET-OF-RELAY-A"     # normal path untouched
    assert c.members["tuned"].timeout == 30                   # non-connection overrides kept
    assert c.members["tuned"].headers == {"X-Trace": "other"}
    assert c.members["tuned"].api_key == "SECRET-OF-RELAY-A"
    assert c.members["solo"].configured and c.members["solo"].enabled
    assert c.ids == ["ok", "tuned", "solo"], c.ids
    print("ok  connection atomicity (a provider's key cannot follow another endpoint)")


def test_proxy_policy() -> None:
    """A member can opt out of the environment's proxy, or name its own."""
    from model_council.providers import _client

    doc = {
        "providers": {
            "corp": {"base_url": "https://internal.example/v1", "api_key": "k",
                     "proxy": False},
        },
        "members": [
            {"id": "inherit", "base_url": "https://a.example/v1", "api_key": "k"},
            {"id": "internal", "provider": "corp", "model": "m"},
            {"id": "viaproxy", "provider": "corp", "model": "m",
             "proxy": "http://127.0.0.1:8888"},
        ],
    }
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "config.json"
        path.write_text(json.dumps(doc), encoding="utf-8")
        with env(COUNCIL_CONFIG=str(path)):
            c = cfg.load_council()

    assert c.members["inherit"].proxy is None      # omitted: follow the environment
    assert c.members["internal"].proxy is False    # inherited from the provider
    assert c.members["viaproxy"].proxy == "http://127.0.0.1:8888"   # per-member override
    assert not c.warnings, c.warnings              # proxy is not part of the atomic set

    # the client actually reflects the policy
    assert _client(c.members["internal"]).trust_env is False
    assert _client(c.members["inherit"]).trust_env is True

    # and via environment variables
    with env(COUNCIL_MODELS="a,b", A_PROXY="direct", B_PROXY="http://p:3128"):
        e = cfg.load_council()
    assert e.members["a"].proxy is False
    assert e.members["b"].proxy == "http://p:3128"
    print("ok  proxy policy (inherit / direct / explicit, config and env)")


def test_proxy_defaults_and_overrides() -> None:
    """One route for the council, and the seats that take a different one.

    The reason this is not just a per-member field: a council usually splits
    into a majority that needs the proxy and a minority that must not use it (or
    the reverse). Without a default, the majority is retyped on every member,
    and a member added later quietly gets the wrong route.
    """
    doc = {
        "proxy": "http://gateway:3128",
        "providers": {
            "corp": {"base_url": "https://internal.example/v1", "api_key": "k",
                     "proxy": False},
            "public": {"base_url": "https://api.example/v1", "api_key": "k"},
        },
        "members": [
            {"id": "default", "provider": "public", "model": "m"},
            {"id": "internal", "provider": "corp", "model": "m"},
            {"id": "own", "provider": "public", "model": "m",
             "proxy": "http://other:8080"},
            {"id": "follows", "provider": "public", "model": "m", "proxy": "env"},
            {"id": "out", "provider": "corp", "model": "m", "proxy": "direct"},
        ],
    }
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "config.json"
        path.write_text(json.dumps(doc), encoding="utf-8")
        with env(COUNCIL_CONFIG=str(path)):
            c = cfg.load_council()

    assert c.members["default"].proxy == "http://gateway:3128"   # the council's route
    assert c.members["internal"].proxy is False                  # provider beats council
    assert c.members["own"].proxy == "http://other:8080"         # member beats both
    assert c.members["follows"].proxy is None                    # back onto the environment
    assert c.members["out"].proxy is False                       # straight out
    assert not c.warnings, c.warnings

    # and the same three moves through the environment
    with env(COUNCIL_MODELS="a,b,c", COUNCIL_PROXY="http://gateway:3128",
             B_PROXY="direct", C_PROXY="env"):
        e = cfg.load_council()
    assert e.members["a"].proxy == "http://gateway:3128"
    assert e.members["b"].proxy is False
    assert e.members["c"].proxy is None
    print("ok  proxy defaults (council-wide route, overridden per provider and member)")


def test_a_proxy_that_cannot_be_used_is_caught_at_load() -> None:
    """A proxy typo is a configuration error, not one failed call per member.

    httpx rejects these from inside the request, so the member reads as `ready`
    until it is asked, and then reports a ValueError about a URL scheme that
    nobody would connect to the line they typed in a config file. Reading the
    roster is the moment to say it.
    """
    from model_council.providers import _client, _proxy_hint

    doc = {"members": [
        {"id": "noscheme", "base_url": "https://a/v1", "api_key": "k", "model": "m",
         "proxy": "127.0.0.1:7890"},
        {"id": "nonsense", "base_url": "https://a/v1", "api_key": "k", "model": "m",
         "proxy": "gopher://p:70"},
        {"id": "secretive", "base_url": "https://a/v1", "api_key": "k", "model": "m",
         "proxy": "http://user:hunter2@p:3128"},
    ]}
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "config.json"
        path.write_text(json.dumps(doc), encoding="utf-8")
        with env(COUNCIL_CONFIG=str(path)):
            c = cfg.load_council()

    # A bare host:port is how everyone writes a proxy. Assume http, say so, and
    # keep the member — and prove httpx will actually take what we assumed.
    assert c.members["noscheme"].proxy == "http://127.0.0.1:7890"
    assert c.members["noscheme"].enabled
    assert "no scheme" in c.members["noscheme"].proxy_note
    _client(c.members["noscheme"])

    # A scheme nothing can dial is not a route. The member is parked, with the
    # reason where a caller reads it, rather than left to fail every call.
    assert not c.members["nonsense"].enabled
    assert "proxy" in c.members["nonsense"].disabled_reason
    assert "nonsense" not in c.ids and "noscheme" in c.ids, c.ids
    joined = " ".join(c.warnings)
    assert "gopher" in joined and "noscheme" in joined, c.warnings

    # A password in a proxy URL must not reach anything a human will read: the
    # table, the warnings, or the text of a connection error.
    assert cfg.redact_proxy("http://user:hunter2@p:3128") == "http://user:***@p:3128"
    hint = _proxy_hint(c.members["secretive"])
    assert "hunter2" not in hint and "user:***@p:3128" in hint, hint

    # socks needs a package httpx does not install by default; without it the
    # first call raises ImportError from inside a request. Park it here instead.
    if not cfg._has_socks():
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "config.json"
            path.write_text(json.dumps({"members": [
                {"id": "sock", "base_url": "https://a/v1", "api_key": "k", "model": "m",
                 "proxy": "socks5://127.0.0.1:1080"}]}), encoding="utf-8")
            with env(COUNCIL_CONFIG=str(path)):
                sc = cfg.load_council()
        assert not sc.members["sock"].enabled
        assert "socksio" in " ".join(sc.warnings), sc.warnings
    print("ok  proxy diagnosis (scheme fixed or member parked, passwords masked)")


async def test_the_route_is_visible() -> None:
    """A council split across two routes must not read like one that is not.

    Everything else in `list_council` is about which model answers; the route is
    the only line that explains why one member times out while the rest are
    fine, and it is invisible in the config the caller cannot see.
    """
    from model_council import server as s

    def member(i: str, proxy=None) -> cfg.Member:
        return cfg.Member(id=i, model="m", base_url="https://h/v1", api_key="k",
                          label=i, proxy=proxy)

    saved = s.COUNCIL
    try:
        # Nothing configured, nothing in the environment: every member goes
        # straight out, and a column saying so ten times is noise.
        s.COUNCIL = cfg.Council({"a": member("a"), "b": member("b")}, "test")
        with env():
            plain = await s.list_council()
        assert "route" not in plain and "network:" not in plain, plain

        # A proxy in the environment applies to everyone, so say which one.
        with env(HTTPS_PROXY="http://127.0.0.1:7890"):
            followed = await s.list_council()
        assert "route" in followed and followed.count("env") >= 2, followed
        assert "HTTPS_PROXY=http://127.0.0.1:7890" in followed, followed

        # One member off on its own is exactly the case the column exists for.
        s.COUNCIL = cfg.Council(
            {"a": member("a"), "b": member("b", "http://user:hunter2@box:3128"),
             "c": member("c", False)}, "test")
        with env():
            split = await s.list_council()
        assert "http://user:***@box:3128" in split and "hunter2" not in split, split
        assert "direct" in split, split
    finally:
        s.COUNCIL = saved
    print("ok  list_council shows the route, and only when the routes can differ")


def test_retry_settings() -> None:
    """The retry policy is inheritable, overridable per member, and clamped."""
    doc = {
        "retries": 1,
        "providers": {
            "flaky": {"base_url": "https://f/v1", "api_key": "k",
                      "retries": 4, "retry_backoff": 0.5},
        },
        "members": [
            {"id": "inherit", "provider": "flaky", "model": "m"},
            {"id": "once", "provider": "flaky", "model": "m", "retries": 0},
            {"id": "greedy", "base_url": "https://g/v1", "api_key": "k", "model": "m",
             "retries": 99},
            {"id": "plain", "base_url": "https://p/v1", "api_key": "k", "model": "m"},
        ],
    }
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "config.json"
        path.write_text(json.dumps(doc), encoding="utf-8")
        with env(COUNCIL_CONFIG=str(path)):
            c = cfg.load_council()

    assert not c.warnings, c.warnings                  # retry fields are not typos
    assert c.members["inherit"].retries == 4           # from the provider
    assert c.members["inherit"].retry_backoff == 0.5
    assert c.members["once"].retries == 0              # single-shot on request
    assert c.members["greedy"].retries == cfg._MAX_RETRIES   # a typo cannot cost 100 calls
    assert c.members["plain"].retries == 1             # the file's top-level default

    with env(COUNCIL_MODELS="a,b", COUNCIL_RETRIES="3", B_RETRIES="0", B_RETRY_BACKOFF="2"):
        e = cfg.load_council()
    assert (e.members["a"].retries, e.members["a"].retry_backoff) == (3, 1.0)
    assert (e.members["b"].retries, e.members["b"].retry_backoff) == (0, 2.0)

    with env():
        assert cfg.load_council().members["glm"].retries == 2   # built-in default
    print("ok  retry settings (provider inheritance, per-member override, clamp)")


def test_weights() -> None:
    """Weights are per member, default to 1, and cannot be set to nonsense."""
    doc = {
        "providers": {"relay": {"base_url": "https://r/v1", "api_key": "k",
                                "weight": 5}},
        "members": [
            {"id": "strong", "provider": "relay", "model": "m", "weight": 2.5},
            {"id": "plain", "provider": "relay", "model": "m"},
            {"id": "advisory", "provider": "relay", "model": "m", "weight": 0},
            {"id": "greedy", "provider": "relay", "model": "m", "weight": 1e9},
            {"id": "negative", "provider": "relay", "model": "m", "weight": -3},
            {"id": "typo", "provider": "relay", "model": "m", "weight": "high"},
        ],
    }
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "config.json"
        path.write_text(json.dumps(doc), encoding="utf-8")
        with env(COUNCIL_CONFIG=str(path)):
            c = cfg.load_council()

    assert c.members["strong"].weight == 2.5
    assert c.members["plain"].weight == 1.0            # not inherited from the provider
    assert any("weight" in w for w in c.warnings), c.warnings   # it is not a provider field
    assert c.members["advisory"].weight == 0.0         # answers, but counts for nothing
    assert c.members["greedy"].weight == cfg._MAX_WEIGHT
    assert c.members["negative"].weight == 0.0         # a weight below zero is meaningless
    assert c.members["typo"].weight == 1.0             # neutral, and visible in list_council

    with env(COUNCIL_MODELS="a,b,c", A_WEIGHT="3", B_WEIGHT=""):
        e = cfg.load_council()
    assert (e.members["a"].weight, e.members["b"].weight, e.members["c"].weight) == (3, 1, 1)
    print("ok  weights (per member, default 1, clamped, typos fall back to neutral)")


async def test_the_ceiling_is_on_the_call_not_the_discussion() -> None:
    """Hitting MAX_ROUNDS must not read as the discussion having no rounds left."""
    from model_council import server as s
    from model_council.providers import Answer

    def member(i: str) -> cfg.Member:
        return cfg.Member(id=i, model="m", base_url="https://h/v1", api_key="k", label=i)

    async def answering(m, prompt, system=None, docs=None):
        return Answer(m, f"{m.label} still thinks so", ok=True)

    saved = s.COUNCIL, s.ask_member
    try:
        s.ask_member = answering
        s.COUNCIL = cfg.Council({"a": member("a"), "b": member("b")}, "test")

        out = await s.ask_all("q", rounds=s.MAX_ROUNDS)
        assert f"ROUND {s.MAX_ROUNDS} of {s.MAX_ROUNDS}" in out, out
        assert "not the most this discussion can have" in out, out
        assert f"round={s.MAX_ROUNDS}" in out, "it should name the next call"

        # Below the ceiling there is no false impression to correct, and a note
        # on every transcript is noise rather than guidance.
        assert "not the most this discussion can have" not in await s.ask_all("q")

        # Nor when the round produced nothing to put in front of anyone.
        async def failing(m, prompt, system=None, docs=None):
            return Answer(m, "[down]", ok=False)
        s.ask_member = failing
        out = await s.ask_all("q", rounds=s.MAX_ROUNDS)
        assert "not the most this discussion can have" not in out, out
    finally:
        s.COUNCIL, s.ask_member = saved
    print("ok  the round ceiling is on one call, and says so")


async def test_weights_reach_the_reader_not_the_members() -> None:
    """A weight is reported with the answers and withheld from the models.

    Both halves matter. The caller cannot weigh opinions it cannot see, and a
    member told it is outranked stops arguing — which would cost exactly the
    dissent the second round exists to surface.
    """
    from model_council import server as s
    from model_council.providers import Answer

    def member(mid: str, **kw) -> cfg.Member:
        return cfg.Member(id=mid, model=f"m-{mid}", base_url="https://h/v1",
                          api_key="k", label=mid.title(), **kw)

    alpha, beta = member("alpha", weight=3), member("beta", weight=0.5)
    prompts: list[str] = []

    async def answering(m, prompt, system=None, docs=None):
        prompts.append(prompt)
        return Answer(m, f"{m.label} says so", ok=True)

    saved = s.COUNCIL, s.ask_member
    try:
        s.ask_member = answering
        s.COUNCIL = cfg.Council({"alpha": alpha, "beta": beta}, "test")
        out = await s.ask_all("Why is the sky blue?", rounds=2)
        assert "weight 3" in out and "weight 0.5" in out, out
        assert "[WEIGHTS — Alpha 3, Beta 0.5]" in out, out          # ranked, highest first
        assert "not votes" in out, "the caller was given a ranking and no way to read it"
        assert "Weight 0 is advisory" not in out, "nobody here has weight 0"
        assert not any("weight" in p.lower() for p in prompts), \
            "a member was told how the council ranks it"

        # Equal weights say nothing, so they are not mentioned at all.
        s.COUNCIL = cfg.Council({"alpha": member("alpha"), "beta": member("beta")}, "test")
        plain = await s.ask_all("Why is the sky blue?")
        assert "weight" not in plain.lower(), plain

        s.COUNCIL = cfg.Council({"alpha": alpha, "beta": member("beta", weight=0)}, "test")
        zeroed = await s.ask_all("Why is the sky blue?")
        assert "Weight 0 is advisory" in zeroed, zeroed
    finally:
        s.COUNCIL, s.ask_member = saved
    print("ok  weights (labeled for the caller, never shown to the members)")


# --------------------------------------------------------------------------- #
# A stub endpoint, so retry behaviour can be tested without a network.
# --------------------------------------------------------------------------- #
def _ok() -> httpx.Response:
    return httpx.Response(200, json={"choices": [{"message": {"content": "pong"}}]})


def _status(code: int, headers: dict | None = None):
    return lambda: httpx.Response(code, text="busy", headers=headers or {})


def _boom():
    def make() -> httpx.Response:
        raise httpx.ConnectError("boom")
    return make


@contextmanager
def serving(*outcomes):
    """Serve the given outcomes in order, repeating the last once they run out.

    Yields the list of requests actually made, which is the thing under test:
    how many attempts a given failure was worth.
    """
    from model_council import providers as p

    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return outcomes[min(len(seen) - 1, len(outcomes) - 1)]()

    saved = p._client
    p._client = lambda m, timeout=None: httpx.AsyncClient(
        transport=httpx.MockTransport(handler))
    try:
        yield seen
    finally:
        p._client = saved


async def test_retry_transport() -> None:
    """Transient failures get another attempt; permanent ones do not."""
    from model_council.providers import ask_member

    def member(**kw) -> cfg.Member:
        # retry_backoff=0 keeps the test instant — the sleep still happens.
        return cfg.Member(id="x", model="m", base_url="https://h/v1", api_key="k",
                          label="X", **{"retry_backoff": 0, **kw})

    with serving(_status(503), _status(503), _ok) as seen:
        a = await ask_member(member(), "hi")
    assert a.ok and a.text == "pong" and a.attempts == 3, a
    assert len(seen) == 3, len(seen)

    with serving(_status(401)) as seen:
        a = await ask_member(member(), "hi")
    assert not a.ok and "HTTP 401" in a.text, a.text
    assert len(seen) == 1, "a rejected key was asked again"

    with serving(_boom()) as seen:
        a = await ask_member(member(), "hi")
    assert not a.ok and "gave up after 3 attempts" in a.text, a.text
    assert len(seen) == 3, len(seen)

    # The endpoint's own pacing wins over our backoff curve — but one longer
    # than we are willing to wait ends the call instead of burning the
    # remaining attempts to be told 'no' again.
    with serving(_status(429, {"retry-after": "3600"})) as seen:
        a = await ask_member(member(), "hi")
    assert len(seen) == 1 and "3600s" in a.text, (len(seen), a.text)

    with serving(_status(429, {"retry-after": "0"}), _ok) as seen:
        a = await ask_member(member(), "hi")
    assert a.ok and len(seen) == 2, (a, len(seen))

    with serving(_status(503)) as seen:                 # opting out entirely
        a = await ask_member(member(retries=0), "hi")
    assert len(seen) == 1 and "gave up" not in a.text, (len(seen), a.text)

    # A 200 carrying no usable answer is a failure, not an answer — and not
    # one worth paying to repeat.
    with serving(lambda: httpx.Response(200, json={"choices": []})) as seen:
        a = await ask_member(member(), "hi")
    assert not a.ok and len(seen) == 1, (a, len(seen))
    assert "X" in a.text and "unexpected response shape" in a.text, a.text
    print("ok  retry transport (5xx and network retried, 4xx and bad payloads not)")


async def test_rounds_carry_the_previous_answers() -> None:
    """A second round is only a discussion if round 1 travels with it."""
    from model_council import server as s
    from model_council.providers import Answer

    alpha = cfg.Member(id="a", model="m-a", base_url="https://h/v1", api_key="k", label="Alpha")
    beta = cfg.Member(id="b", model="m-b", base_url="https://h/v1", api_key="k", label="Beta")
    seen: list[tuple[str, str]] = []

    async def answering(m, prompt, system=None, docs=None):
        seen.append((m.id, prompt))
        n = sum(1 for i, _ in seen if i == m.id)
        return Answer(m, f"{m.label} says {n}", ok=True)

    async def only_alpha_answers(m, prompt, system=None, docs=None):
        seen.append((m.id, prompt))
        return Answer(m, f"{m.label} answer", ok=(m.id == "a"))

    saved = s.COUNCIL, s.ask_member
    s.COUNCIL = cfg.Council({"a": alpha, "b": beta}, "test")
    try:
        s.ask_member = answering
        one = await s.ask_all("Why is the sky blue?")
        assert "ROUND" not in one, "a single round should look exactly as it always did"
        assert "Alpha says 1" in one and "Beta says 1" in one, one
        assert [p for _, p in seen] == ["Why is the sky blue?"] * 2

        seen.clear()
        two = await s.ask_all("Why is the sky blue?", rounds=2)
        assert len(seen) == 4, seen
        round2 = dict(seen[2:])
        assert "Why is the sky blue?" in round2["a"], "the question was not restated"
        assert "Alpha says 1" in round2["a"], "Alpha never saw its own first answer"
        assert "Beta says 1" in round2["a"], "Alpha never saw the other answer"
        assert "Beta says 1" in round2["b"] and "Alpha says 1" in round2["b"]
        assert "ROUND 1 of 2" in two and "ROUND 2 of 2" in two, two
        assert "Alpha says 2" in two and "Alpha says 1" in two, "a round is missing"

        seen.clear()                                   # clamped, not unbounded
        await s.ask_all("q", rounds=9)
        assert len(seen) == 2 * s.MAX_ROUNDS, len(seen)

        seen.clear()                                   # nothing to discuss
        s.ask_member = only_alpha_answers
        stopped = await s.ask_all("q", rounds=2)
        assert len(seen) == 2, "a second round ran with only one answer to show"
        assert "stopped after round 1" in stopped, stopped
    finally:
        s.COUNCIL, s.ask_member = saved
    print("ok  rounds (round 2 carries every round-1 answer, and stops when it cannot)")


async def test_guests_join_the_argument() -> None:
    """An answer the caller already holds is seated, and argued with.

    The point is not that it shows up in the transcript — that would be a
    formatting change. It is that the members are handed it verbatim and asked
    to respond, which is the whole difference between being in the discussion
    and being printed next to it.
    """
    from model_council import server as s
    from model_council.providers import Answer

    def member(mid: str, label: str, **kw) -> cfg.Member:
        return cfg.Member(id=mid, model=f"m-{mid}", base_url="https://h/v1",
                          api_key="k", label=label, **kw)

    calls: list[tuple[str, str]] = []

    async def answering(m, prompt, system=None, docs=None):
        calls.append((m.id, prompt))
        n = sum(1 for i, _ in calls if i == m.id)
        return Answer(m, f"{m.label} round {n}", ok=True)

    saved = s.COUNCIL, s.ask_member
    guest = s.Guest(label="Subagent", text="the migration has no rollback path")
    try:
        s.ask_member = answering
        s.COUNCIL = cfg.Council({"a": member("a", "Alpha"), "b": member("b", "Beta")}, "t")

        two = await s.ask_all("Any traps here?", rounds=2, guests=[guest])
        assert "Subagent (supplied by the caller)" in two, two
        assert "the migration has no rollback path" in two

        # The load-bearing assertion: both members were shown the guest's text
        # in round 2, verbatim, and told it will not answer back.
        round2 = {i: p for i, p in calls[2:]}
        assert len(calls) == 4, calls
        for who in ("a", "b"):
            assert "the migration has no rollback path" in round2[who], \
                f"{who} never saw the guest's answer"
            assert "does not revise between rounds" in round2[who], round2[who]
        assert not any(i.startswith("guest") for i, _ in calls), "a guest was called"

        # It speaks once: round 2 prints the members only, and says why.
        r2 = two.split("ROUND 2")[1]
        # The answer-block form specifically: the guest is legitimately named in
        # the letter key and in the note explaining its absence, and neither is
        # it having spoken again.
        assert "===== Subagent" not in r2, \
            "the guest was reprinted as though it had answered again"
        assert "not a position withdrawn" in two, two

        # One round has no later round to explain, so it stays quiet.
        calls.clear()
        one = await s.ask_all("Any traps here?", guests=[guest])
        assert "Subagent (supplied by the caller)" in one
        assert "speaks once" not in one, one

        # A guest is enough of a second voice to make one member worth a round 2.
        calls.clear()
        s.COUNCIL = cfg.Council({"a": member("a", "Alpha")}, "t")
        solo = await s.ask_all("Any traps here?", rounds=2, guests=[guest])
        assert "stopped after round 1" not in solo, solo
        assert "the migration has no rollback path" in calls[1][1], \
            "the lone member was not shown the guest it was meant to argue with"

        # Weights put guests on the same scale as members.
        calls.clear()
        s.COUNCIL = cfg.Council({"a": member("a", "Alpha", weight=2)}, "t")
        ranked = await s.ask_all("q", guests=[s.Guest(label="Sub", text="x", weight=0.5)])
        assert "[WEIGHTS — Alpha 2, Sub 0.5]" in ranked, ranked
        assert "weight 0.5" in ranked, ranked

        # An empty answer is not a participant.
        calls.clear()
        blank = await s.ask_all("q", guests=[s.Guest(label="Empty", text="   ")])
        assert "Empty" not in blank, blank

        # Nobody to argue with is a mistake worth naming, not a transcript of one.
        s.COUNCIL = cfg.Council({}, "t")
        alone = await s.ask_all("q", guests=[guest])
        assert "nobody to put these answers in front of" in alone, alone
    finally:
        s.COUNCIL, s.ask_member = saved
    print("ok  guests (seated, shown to the members verbatim, and argued with)")


async def test_revise_drives_a_round_from_outside() -> None:
    """One round, driven by the caller, so a voice only it can produce keeps up.

    The distinction being tested is the whole feature: a `revise` outsider is
    presented to the members exactly as another member is, because it *will*
    answer again — while an `ask_all` guest is flagged as finished, because it
    will not. Getting that backwards misrepresents the discussion to the models.
    """
    from model_council import server as s
    from model_council.providers import Answer

    def member(mid: str, label: str, **kw) -> cfg.Member:
        return cfg.Member(id=mid, model=f"m-{mid}", base_url="https://h/v1",
                          api_key="k", label=label, **kw)

    calls: list[tuple[str, str]] = []

    async def answering(m, prompt, system=None, docs=None):
        calls.append((m.id, prompt))
        return Answer(m, f"{m.label} revised", ok=True)

    saved = s.COUNCIL, s.ask_member
    try:
        s.ask_member = answering
        s.COUNCIL = cfg.Council({"a": member("a", "Alpha"), "b": member("b", "Beta")}, "t")

        out = await s.revise(
            prompt="Any traps?",
            # Name-free on purpose: the labels are anonymised, but the prose is
            # passed through untouched, so a fixture that says "Subagent said"
            # would defeat the leak check below and prove nothing.
            answers=[s.PriorAnswer(model="a", text="indexes will degrade"),
                     s.PriorAnswer(model="b", text="concurrent writes race"),
                     s.PriorAnswer(label="Subagent", text="the migration has no rollback")],
            round=1)

        seen = dict(calls)
        assert len(calls) == 2, calls
        # A named member gets its own answer back as its own — that is what makes
        # this a revision rather than the same question asked twice.
        assert "YOUR OWN ROUND-1 ANSWER" in seen["a"] and "indexes will degrade" in seen["a"]
        assert "concurrent writes race" in seen["a"]
        # And the outsider is just another member as far as the models can tell —
        # since anonymity, literally so: a letter like everyone else.
        assert "ROUND-1 ANSWER C" in seen["a"], seen["a"]
        assert "Subagent" not in seen["a"], "the outsider was named to a member"
        assert "does not revise" not in seen["a"], \
            "the subagent was announced as finished, but it answers again next round"
        assert "Alpha revised" in out and "Beta revised" in out
        assert "ROUND 2" in out and "at the table: Alpha, Beta, Subagent" in out, out

        # An `ask_all` guest is the opposite case, and must stay that way.
        calls.clear()
        await s.ask_all("Any traps?", rounds=2,
                        guests=[s.Guest(label="Oneshot", text="a single remark")])
        assert "does not revise between rounds" in dict(calls[2:])["a"], \
            "a guest that cannot answer again was presented as though it would"

        # Round numbers follow the caller's loop.
        calls.clear()
        third = await s.revise(prompt="q", round=2, answers=[
            s.PriorAnswer(model="a", text="x"), s.PriorAnswer(label="Sub", text="y")])
        assert "ROUND 3" in third and "you are now in round 3" in dict(calls)["a"]

        # Half a table is not a discussion, and silence about it would be worse.
        thin = await s.revise(prompt="q", answers=[s.PriorAnswer(model="a", text="x")])
        assert "at least two answers" in thin, thin

        # A stale or mistyped id still contributes its text, but the member it
        # named never sees it as its own — say so rather than quietly dropping it.
        calls.clear()
        typo = await s.revise(prompt="q", answers=[
            s.PriorAnswer(model="a", text="x"),
            s.PriorAnswer(model="nope", text="orphaned text"),
            s.PriorAnswer(model="b", text="z")])
        assert "not on this council: nope" in typo, typo
        assert "orphaned text" in dict(calls)["a"], "the answer was silently dropped"

        # Outside voices are weighed on the members' scale.
        calls.clear()
        s.COUNCIL = cfg.Council({"a": member("a", "Alpha", weight=2)}, "t")
        ranked = await s.revise(prompt="q", answers=[
            s.PriorAnswer(model="a", text="x"),
            s.PriorAnswer(label="Sub", text="y", weight=0.5)])
        assert "[WEIGHTS — Alpha 2, Sub 0.5]" in ranked, ranked
    finally:
        s.COUNCIL, s.ask_member = saved
    print("ok  revise (caller-driven round; an outside voice reads as a full member)")


async def test_revision_prompt_writes_for_the_seat_we_cannot_ask() -> None:
    """The caller's own seat gets word-for-word what the members got.

    Without this the caller improvises, and the likeliest improvisation — hand
    the subagent the original question again — reproduces its previous answer
    while the transcript still reads like a discussion. The failure is silent,
    so both halves are tested: the prompt itself, and the warning that says so.
    """
    from model_council import server as s
    from model_council.providers import Answer

    def member(mid: str, label: str) -> cfg.Member:
        return cfg.Member(id=mid, model=f"m-{mid}", base_url="https://h/v1",
                          api_key="k", label=label)

    async def answering(m, prompt, system=None, docs=None):
        return Answer(m, f"{m.label} revised", ok=True)

    prior = [s.PriorAnswer(model="a", text="Alpha said indexes degrade"),
             s.PriorAnswer(model="b", text="Beta said writes race"),
             s.PriorAnswer(label="Subagent", text="Subagent said no rollback")]

    saved = s.COUNCIL, s.ask_member
    try:
        s.ask_member = answering
        s.COUNCIL = cfg.Council({"a": member("a", "Alpha"), "b": member("b", "Beta")}, "t")

        mine = await s.revision_prompt(prompt="Any traps?", answers=prior,
                                       seat="Subagent", round=1)
        assert "YOUR OWN ROUND-1 ANSWER" in mine and "no rollback" in mine, mine
        assert "Alpha said indexes degrade" in mine and "Beta said writes race" in mine
        # The sentence that keeps the seat from folding — the members get it, so
        # the subagent must too, or the two are not being asked the same thing.
        assert "just because you are outnumbered" in mine, mine
        # Its own answer appears once, as its own, not also as somebody else's.
        assert mine.count("no rollback") == 1, mine

        # A member id addresses the same seat as its label does.
        by_id = await s.revision_prompt(prompt="Any traps?", answers=prior, seat="a")
        by_label = await s.revision_prompt(prompt="Any traps?", answers=prior, seat="Alpha")
        assert by_id == by_label and "YOUR OWN ROUND-1 ANSWER" in by_id
        assert "indexes degrade" in by_id

        # A seat that sat the round out is told so, not handed someone else's words.
        newcomer = await s.revision_prompt(prompt="Any traps?", answers=prior, seat="Latecomer")
        assert "(you did not answer)" in newcomer, newcomer
        assert "no rollback" in newcomer, "it still needs to see what was said"

        blank = await s.revision_prompt(prompt="q", answers=[], seat="Subagent")
        assert "nothing to revise from" in blank, blank

        # And `revise` itself says the round is not finished, in its own output.
        out = await s.revise(prompt="Any traps?", answers=prior, round=1)
        assert "Subagent did not answer above" in out, out
        assert 'revision_prompt(seat="Subagent"' in out, out
        assert "previous answer again" in out, out
    finally:
        s.COUNCIL, s.ask_member = saved
    print("ok  revision_prompt (the caller's seat is asked on the members' terms)")


async def test_members_argue_with_letters_not_brands() -> None:
    """The members debate anonymously; the caller alone gets the key.

    A model name is the same kind of signal as a weight, and a stronger one:
    models hold priors about each other's makers. Withholding the number while
    printing the brand would leave the larger channel open. The caller is the
    exception because it is the one who has to tell them apart afterwards.
    """
    from model_council import server as s
    from model_council.providers import Answer

    def member(mid: str, label: str) -> cfg.Member:
        return cfg.Member(id=mid, model=f"m-{mid}", base_url="https://h/v1",
                          api_key="k", label=label)

    calls: list[tuple[str, str]] = []

    async def answering(m, prompt, system=None, docs=None):
        calls.append((m.id, prompt))
        n = sum(1 for i, _ in calls if i == m.id)
        return Answer(m, f"{m.label} round {n}", ok=True)

    brands = ("GLM-5.3", "GPT-5.6-sol")
    saved = s.COUNCIL, s.ask_member
    try:
        s.ask_member = answering
        s.COUNCIL = cfg.Council({"a": member("a", brands[0]),
                                 "b": member("b", brands[1])}, "t")
        prior = [s.PriorAnswer(model="a", text="indexes degrade"),
                 s.PriorAnswer(model="b", text="writes race"),
                 s.PriorAnswer(label="Subagent", text="no rollback")]

        out = await s.revise(prompt="Any traps?", answers=prior, round=1)
        seen = dict(calls)

        # Nobody's prompt may contain anybody's name.
        for who, prompt in seen.items():
            for brand in (*brands, "Subagent"):
                assert brand not in prompt, f"{who} was shown the name {brand}"
            assert "YOUR OWN ROUND-1 ANSWER" in prompt      # its own is still its own
            assert "judge them by their reasoning" in prompt

        # Letters are positional, so one letter is one model for everyone —
        # otherwise "B was wrong" means different things to different members.
        assert "ROUND-1 ANSWER B" in seen["a"] and "ROUND-1 ANSWER C" in seen["a"]
        assert "ROUND-1 ANSWER A" in seen["b"] and "ROUND-1 ANSWER C" in seen["b"]
        assert "ANSWER A" not in seen["a"], "a member was handed its own answer twice"
        assert "ANSWER B" not in seen["b"], "a member was handed its own answer twice"
        # Every seat's text still travels, whatever it is called.
        for prompt in seen.values():
            assert "no rollback" in prompt and "indexes degrade" in prompt

        # The caller keeps the names, and is given the key to read the argument.
        assert all(b in out for b in brands), out
        assert "A = GLM-5.3, B = GPT-5.6-sol, C = Subagent" in out, out

        # A guest is still marked as finished — that is process, not identity.
        calls.clear()
        await s.ask_all("Any traps?", rounds=2,
                        guests=[s.Guest(label="Oneshot", text="a single remark")])
        guest_round2 = dict(calls[2:])["a"]
        assert "does not revise between rounds" in guest_round2
        assert "Oneshot" not in guest_round2, "the guest's name leaked"

        # Round 1 alone shows nobody anything, so there is no key to explain.
        calls.clear()
        one = await s.ask_all("Any traps?")
        assert "saw each other as letters" not in one, one

        # The honest limit, asserted rather than hoped for: anonymity covers the
        # labels this server writes, not a name a model puts in its own prose.
        # That text is carried verbatim — scrubbing it would mean editing an
        # answer the members are supposed to be critiquing.
        calls.clear()
        leaky = [s.PriorAnswer(model="a", text="as GPT-5.6-sol argued, indexes degrade"),
                 s.PriorAnswer(model="b", text="writes race")]
        await s.revise(prompt="Any traps?", answers=leaky, round=1)
        assert "GPT-5.6-sol" in dict(calls)["b"], \
            "an answer was edited on its way to the other members"

        # The caller's own seat is written on the same terms as the members'.
        mine = await s.revision_prompt(prompt="Any traps?", answers=prior,
                                       seat="Subagent", round=1)
        assert "ROUND-1 ANSWER A" in mine and "ROUND-1 ANSWER B" in mine, mine
        assert not any(b in mine for b in brands), "the caller's seat was shown brands"
        assert "YOUR OWN ROUND-1 ANSWER" in mine and "no rollback" in mine
    finally:
        s.COUNCIL, s.ask_member = saved
    print("ok  anonymity (members argue with letters; only the caller gets names)")


def test_registry_metadata_agrees() -> None:
    """server.json must name the same server the README claims ownership of,
    and every file that records a version must record the same one.

    The registry verifies ownership by finding `mcp-name: <name>` in the README
    that PyPI shows; if the two ever disagree, publishing fails after the PyPI
    upload has already happened, which is the worst moment to find out.

    The version lives in four places, and __init__ is the one that is easy to
    forget because nothing downstream reads it back: it is what the server
    advertises in the MCP handshake, so a stale value ships a package that
    misreports itself to every client, and PyPI will not take the version
    number back to fix it.
    """
    import model_council

    root = Path(__file__).resolve().parent.parent
    server_json = root / "server.json"
    if not server_json.exists():
        print("--  skipped registry metadata check (server.json not present)")
        return
    doc = json.loads(server_json.read_text(encoding="utf-8"))
    readme = (root / "README.md").read_text(encoding="utf-8")
    version = next(l.split('"')[1] for l in
                   (root / "pyproject.toml").read_text(encoding="utf-8").splitlines()
                   if l.startswith("version = "))

    manifest = root / "manifest.json"
    if manifest.exists():   # the desktop-extension bundle ships the same version
        assert json.loads(manifest.read_text(encoding="utf-8"))["version"] == version

    assert model_council.__version__ == version, (
        f"__init__ says {model_council.__version__}, pyproject says {version} — the "
        f"server would advertise the wrong version to every client")

    assert f"mcp-name: {doc['name']}" in readme, "README is missing the ownership marker"
    assert doc["version"] == version, (doc["version"], version)
    pkg = doc["packages"][0]
    assert pkg["version"] == version, (pkg["version"], version)
    assert pkg["registryType"] == "pypi" and pkg["identifier"] == "model-council-mcp", pkg
    assert len(doc["description"]) <= 100, len(doc["description"])
    print("ok  registry metadata (server.json, README marker and version agree)")


def test_shipped_example_is_valid() -> None:
    """The example we hand users must load cleanly — no warnings, no typos."""
    example = Path(__file__).resolve().parent.parent / "examples" / "config.json"
    if not example.exists():
        print("--  skipped shipped-example check (examples/ not present)")
        return
    with env(COUNCIL_CONFIG=str(example), MY_RELAY_KEY="sk-a", GLM_KEY="sk-g",
             KIMI_KEY="sk-k", INTERNAL_KEY="sk-i", SPARE_KEY="sk-s"):
        c = cfg.load_council()
    assert not c.warnings, c.warnings
    assert c.ids == ["gpt5", "codex", "glm", "small", "sol", "kimi",
                     "inhouse"], c.ids            # 'spare' is off, and a backup is not an id
    sol = c.members["sol"]
    assert [b.model for b in sol.backups] == ["gpt-5-codex", "gpt-5-codex-latest"]
    assert sol.proxy is False and all(b.proxy is None for b in sol.backups)
    assert all(c.members[i].configured for i in c.ids), c.members
    assert c.members["inhouse"].proxy is False        # inherited from its provider
    assert c.members["kimi"].proxy == "http://127.0.0.1:7890"   # its own route
    assert c.members["gpt5"].proxy is None            # everyone else follows the env
    assert not c.members["small"].vision and not c.members["small"].cache
    assert c.members["gpt5"].vision and c.members["gpt5"].cache   # both default on
    print("ok  examples/config.json loads with no warnings")


def test_explicit_beats_discovered() -> None:
    """A roster passed by the client must not be shadowed by a stray config file.

    Someone who fills in a desktop-extension form while an old
    ~/.config/model-council/config.json is still lying around should get what
    they typed, not the leftover file, and with no way to tell from the outside.
    """
    doc = {"members": [{"id": "leftover", "base_url": "https://old.example/v1",
                        "api_key": "k", "model": "m"}]}
    with tempfile.TemporaryDirectory() as d:
        discovered = Path(d) / "config.json"
        discovered.write_text(json.dumps(doc), encoding="utf-8")

        # discovered file wins when nothing more explicit is given
        with env():
            cfg.DEFAULT_CONFIG_PATH = discovered
            c = cfg.load_council()
        assert c.ids == ["leftover"], c.ids

        # an explicit roster beats it
        with env(COUNCIL_MODELS="typed", TYPED_BASE_URL="https://new.example/v1",
                 TYPED_API_KEY="k", TYPED_MODEL="m"):
            cfg.DEFAULT_CONFIG_PATH = discovered
            c = cfg.load_council()
        assert c.ids == ["typed"], c.ids
        assert "environment" in c.source, c.source

        # an explicit file beats both
        other = Path(d) / "other.json"
        other.write_text(json.dumps(
            {"members": [{"id": "pointed", "base_url": "https://p.example/v1",
                          "api_key": "k", "model": "m"}]}), encoding="utf-8")
        with env(COUNCIL_CONFIG=str(other), COUNCIL_MODELS="typed"):
            cfg.DEFAULT_CONFIG_PATH = discovered
            c = cfg.load_council()
        assert c.ids == ["pointed"], c.ids
    print("ok  source precedence (explicit config beats a discovered file)")


def test_missing_file_falls_back() -> None:
    with env(COUNCIL_CONFIG="/nope/config.json"):
        c = cfg.load_council()
    assert c.members and "environment" in c.source, c.source
    assert any("does not exist" in w for w in c.warnings), c.warnings
    print("ok  missing config file degrades to env instead of crashing")


async def test_tools() -> None:
    from model_council import server as s

    names = sorted(t.name for t in await s.mcp.list_tools())
    assert names == ["ask", "ask_all", "list_council", "probe_models", "revise",
                     "revision_prompt"], names

    # The roster is a schema constraint, not a suggestion in the prose: a wrong
    # id is rejected by validation before any tool body runs.
    tools = {t.name: t for t in await s.mcp.list_tools()}
    model_schema = tools["ask"].input_schema["properties"]["model"]
    assert model_schema.get("enum") == s.COUNCIL.ids, model_schema
    subset = tools["ask_all"].input_schema["properties"]["models"]
    assert any(o.get("items", {}).get("enum") == s.COUNCIL.ids
               for o in subset["anyOf"]), subset
    assert tools["ask_all"].input_schema["properties"]["rounds"]["default"] == 1

    bad = await s.ask(model="does-not-exist", prompt="hi")
    assert "unknown model id" in bad, bad      # the fallback path still holds

    print("ok  tool surface:", names)
    print()
    print(await s.list_council())

    if any(m.configured for m in s.COUNCIL.members.values()):
        print("\nLive round-trip via ask_all:\n")
        print(await s.ask_all("Reply with exactly one word: pong"))
    else:
        print("\n(no live keys configured — skipped the network round-trip)")


def test_allowlist_parsing() -> None:
    from model_council.access import parse_networks

    assert [str(n) for n in parse_networks("loopback")] == ["127.0.0.0/8", "::1/128"]
    assert str(parse_networks("private")[0]) == "10.0.0.0/8"
    # Commas, whitespace and a mix of forms all arrive from different config
    # files, and must land in the same place.
    got = [str(n) for n in parse_networks(" 10.20.0.0/16,  10.30.1.5 ")]
    assert got == ["10.20.0.0/16", "10.30.1.5/32"], got
    assert parse_networks("") == []
    for bad in ("nonsense", "10.0.0.0/99", "10.0.0.300"):
        try:
            parse_networks(bad)
        except ValueError as e:
            assert "private" in str(e), e          # the message lists what is valid
        else:
            raise AssertionError(f"{bad!r} should not have parsed")
    print("ok  allowlist parsing (CIDRs, bare addresses, aliases, refusals)")


async def _gate_status(gate, peer: str | None, headers: dict | None = None) -> int:
    """Push one request through a ClientGate and report the status it produced."""
    scope = {
        "type": "http", "method": "GET", "path": "/healthz",
        "client": (peer, 51000) if peer else None,
        "headers": [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()],
    }
    seen: list[int] = []

    async def send(msg):
        if msg["type"] == "http.response.start":
            seen.append(msg["status"])

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    await gate(scope, receive, send)
    return seen[0]


async def test_http_gate() -> None:
    from model_council.access import ClientGate, parse_networks

    async def ok_app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    allow = parse_networks("10.0.0.0/8")

    plain = ClientGate(ok_app, allow=allow)
    assert await _gate_status(plain, "10.1.2.3") == 200
    assert await _gate_status(plain, "8.8.8.8") == 403
    assert await _gate_status(plain, "127.0.0.1") == 200      # loopback is implicit
    assert await _gate_status(plain, "::ffff:10.1.2.3") == 200  # dual-stack peer form
    assert await _gate_status(plain, None) == 403             # no peer, no admission

    # Without --trust-proxy the header is not evidence of anything, so an
    # outsider cannot write themselves an address on the allowlist.
    assert await _gate_status(plain, "8.8.8.8", {"X-Forwarded-For": "10.1.2.3"}) == 403

    behind = ClientGate(ok_app, allow=allow, trusted_proxies=parse_networks("192.0.2.9"))
    assert await _gate_status(behind, "192.0.2.9", {"X-Forwarded-For": "10.1.2.3"}) == 200
    assert await _gate_status(behind, "192.0.2.9", {"X-Forwarded-For": "8.8.8.8"}) == 403
    # The rightmost untrusted hop is the client: prepending something allowed to
    # the chain is exactly the spoof this rule exists to defeat.
    assert await _gate_status(
        behind, "192.0.2.9", {"X-Forwarded-For": "10.1.2.3, 8.8.8.8"}) == 403
    assert await _gate_status(
        behind, "192.0.2.9", {"X-Forwarded-For": "8.8.8.8, 10.1.2.3"}) == 200

    # A browser inside the allowlist is still inside the allowlist, so Origin —
    # which MCP clients do not send and pages always do — is what stops it.
    assert await _gate_status(plain, "10.1.2.3", {"Origin": "https://evil.example"}) == 403
    lax = ClientGate(ok_app, allow=allow, allowed_origins=["https://tools.corp"])
    assert await _gate_status(lax, "10.1.2.3", {"Origin": "https://tools.corp"}) == 200
    assert await _gate_status(lax, "10.1.2.3", {"Origin": "https://evil.example"}) == 403

    print("ok  http gate (allowlist, forwarded-for trust, origin refusal)")


def test_http_refuses_to_serve_the_world() -> None:
    """Binding a network without saying who may reach it must not start.

    The allowlist is the only thing standing between shared provider keys and
    whoever finds the port, so it cannot be something you forget.
    """
    from model_council import server as s

    for argv in (["--http", "--host", "0.0.0.0"],
                 ["--http", "--host", "192.168.1.10"],
                 ["--http", "--allow", "bogus"]):
        try:
            s.main(argv)
        except SystemExit as e:
            assert e.code != 0, argv
        else:
            raise AssertionError(f"{argv} should have refused to start")

    # Loopback needs no flag: the bind address is already the limit.
    parser, args = s._parse_args(["--http"])
    assert args.host == "127.0.0.1" and args.allow == ""
    print("ok  http refuses a network bind with no allowlist")


# --------------------------------------------------------------------------- #
# Material
# --------------------------------------------------------------------------- #
_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


def test_material_loading() -> None:
    """What is read, what is refused, and whether the refusal says why."""
    from model_council import materials as mm

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "spec.md").write_text("# Spec\nthe rule is x", encoding="utf-8")
        (root / "shot.png").write_bytes(_PNG)
        (root / "liar.png").write_bytes(b"not a png at all")
        (root / "blob.bin").write_bytes(b"\x00\x01\x02\xff\xfe")

        docs = mm.load([mm.Material(path=str(root / "spec.md")),
                        mm.Material(label="shot", path=str(root / "shot.png")),
                        mm.Material(label="note", text="an inline fragment")])
        assert [d.label for d in docs] == ["spec.md", "shot", "note"], docs
        assert docs[0].text == "# Spec\nthe rule is x" and not docs[0].is_image
        assert docs[1].is_image and docs[1].media_type == "image/png"
        assert base64.b64decode(docs[1].data) == _PNG
        assert docs[2].origin == "" and docs[2].text == "an inline fragment"
        # The describe() line is what the transcript shows the reader.
        assert "spec.md" in docs[0].describe() and "of text" in docs[0].describe()
        # A small file must not round to "0 KB", which reads as an empty one.
        assert "20 B of text" in docs[0].describe(), docs[0].describe()
        assert f"image/png, {len(_PNG)} B" in docs[1].describe(), docs[1].describe()

        def refuses(spec, fragment: str) -> None:
            try:
                mm.load([spec])
            except mm.MaterialError as e:
                assert fragment in str(e), f"{fragment!r} not in {e}"
            else:
                raise AssertionError(f"should have refused: {spec}")

        refuses(mm.Material(path=str(root / "nope.md")), "no such file")
        refuses(mm.Material(path=str(root)), "is not a file")
        refuses(mm.Material(path=str(root / "liar.png")), "not a png image")
        refuses(mm.Material(path=str(root / "blob.bin")), "neither UTF-8 text nor an image")
        refuses(mm.Material(path=str(root / "spec.md"), text="also this"), "both")

        # An unreadable file must not be resolved into a partial council: the
        # question would be answered anyway, about nothing.
        saved = mm.get_policy()
        try:
            mm.set_policy(mm.Policy(allow_paths=False, why="it serves over HTTP"))
            refuses(mm.Material(path=str(root / "spec.md")), "serves over HTTP")
            # ...but inline text still works, which is what an HTTP caller has.
            assert mm.load([mm.Material(text="inline")])[0].text == "inline"

            inside = root / "sub"
            inside.mkdir()
            (inside / "ok.md").write_text("fine", encoding="utf-8")
            mm.set_policy(mm.Policy(allow_paths=True, root=inside))
            assert mm.load([mm.Material(path=str(inside / "ok.md"))])[0].text == "fine"
            refuses(mm.Material(path=str(root / "spec.md")), "outside")

            # A symlink is judged by where it lands, not where it sits.
            (inside / "escape.md").symlink_to(root / "spec.md")
            refuses(mm.Material(path=str(inside / "escape.md")), "outside")
        finally:
            mm.set_policy(saved)

    assert mm.load(None) == [] and mm.load([mm.Material()]) == []
    print("ok  material loading (files, images, caps, and who may read a path)")


async def test_material_reaches_the_wire() -> None:
    """The bytes actually sent: material first, question last, image as an image."""
    from model_council import materials as mm
    from model_council.providers import ask_member

    docs = mm.load([mm.Material(label="Spec", text="the rule is x"),
                    mm.Material(label="Shot", text="")])
    docs = [docs[0], mm.Loaded(label="Shot", media_type="image/png",
                               data=base64.b64encode(_PNG).decode(), size=len(_PNG))]

    def member(fmt: str, **kw) -> cfg.Member:
        return cfg.Member(id="x", model="m", base_url="https://h/v1", api_key="k",
                          label="X", format=fmt, **kw)

    with serving(_ok) as seen:
        await ask_member(member("openai"), "so what breaks?", docs=docs)
    body = json.loads(seen[0].content)["messages"][0]["content"]
    assert [b["type"] for b in body] == ["text", "text", "image_url", "text"], body
    assert "MATERIAL: Spec" in body[0]["text"] and "the rule is x" in body[0]["text"]
    assert body[2]["image_url"]["url"].startswith("data:image/png;base64,"), body[2]
    # Last, always: the question is read by a model that already has the material.
    assert body[-1]["text"] == "so what breaks?", body[-1]

    def anthropic_ok() -> httpx.Response:
        return httpx.Response(200, json={"content": [{"type": "text", "text": "pong"}]})

    with serving(anthropic_ok) as seen:
        await ask_member(member("anthropic"), "so what breaks?", docs=docs)
    body = json.loads(seen[0].content)["messages"][0]["content"]
    assert [b["type"] for b in body] == ["text", "text", "image", "text"], body
    assert body[2]["source"] == {"type": "base64", "media_type": "image/png",
                                 "data": base64.b64encode(_PNG).decode()}
    # One cache breakpoint, at the end of the material — everything before it is
    # what repeats across members and rounds, and nothing after it does.
    assert body[2].get("cache_control") == {"type": "ephemeral"}, body[2]
    assert not any("cache_control" in b for b in (body[0], body[1], body[3])), body

    with serving(anthropic_ok) as seen:
        await ask_member(member("anthropic", cache=False), "q", docs=docs)
    body = json.loads(seen[0].content)["messages"][0]["content"]
    assert not any("cache_control" in b for b in body), "cache: false was ignored"

    # No material: the content stays the bare string it has always been.
    with serving(_ok) as seen:
        await ask_member(member("openai"), "just a question")
    assert json.loads(seen[0].content)["messages"][0]["content"] == "just a question"
    print("ok  material on the wire (ahead of the question, images as images)")


async def test_truncation_is_not_silent() -> None:
    """An answer stopped at a token ceiling must not read as a finished one."""
    from model_council.providers import ask_member

    def cut_openai() -> httpx.Response:
        return httpx.Response(200, json={"choices": [
            {"message": {"content": "half a th"}, "finish_reason": "length"}]})

    def cut_anthropic() -> httpx.Response:
        return httpx.Response(200, json={"content": [{"type": "text", "text": "half a th"}],
                                         "stop_reason": "max_tokens"})

    base = dict(id="x", model="m", base_url="https://h/v1", api_key="k", label="X")
    with serving(cut_openai):
        a = await ask_member(cfg.Member(**base), "hi")
    # Still an answer — it is real text, and the next round should read it.
    assert a.ok and a.truncated and a.text.startswith("half a th"), a
    assert "cut off" in a.text and "incomplete" in a.text, a.text

    with serving(cut_anthropic):
        a = await ask_member(cfg.Member(**base, format="anthropic", max_tokens=64), "hi")
    assert a.ok and a.truncated and "max_tokens is 64" in a.text, a.text

    with serving(_ok):
        a = await ask_member(cfg.Member(**base), "hi")
    assert a.ok and not a.truncated and a.text == "pong", a
    print("ok  truncation (an answer cut at max_tokens says so, and still counts)")


async def test_material_travels_every_round() -> None:
    """Members are stateless, so round 2 gets the same bytes as round 1."""
    from model_council import materials as mm
    from model_council import server as s
    from model_council.providers import Answer

    alpha = cfg.Member(id="a", model="m", base_url="https://h/v1", api_key="k", label="Alpha")
    beta = cfg.Member(id="b", model="m", base_url="https://h/v1", api_key="k", label="Beta")
    blind = cfg.Member(id="c", model="m", base_url="https://h/v1", api_key="k",
                       label="Cyclops", vision=False)

    seen: list[tuple[str, tuple[str, ...]]] = []

    async def answering(m, prompt, system=None, docs=None):
        seen.append((m.id, tuple(d.label for d in (docs or []))))
        return Answer(m, f"{m.label} says so", ok=True)

    saved = s.COUNCIL, s.ask_member
    try:
        s.ask_member = answering
        with tempfile.TemporaryDirectory() as tmp:
            spec = Path(tmp) / "spec.md"
            spec.write_text("the rule is x", encoding="utf-8")

            s.COUNCIL = cfg.Council({"a": alpha, "b": beta}, "test")
            out = await s.ask_all("what breaks?", rounds=2,
                                  materials=[mm.Material(label="Spec", path=str(spec))])
            assert seen == [("a", ("Spec",)), ("b", ("Spec",)),
                            ("a", ("Spec",)), ("b", ("Spec",))], seen
            assert "material on the table: Spec" in out, out
            assert str(spec) in out, "the transcript should say what was read"

            # An image excludes the members configured as unable to see it, and
            # says which — a seat that answers about a screenshot it never saw
            # would read exactly like one that did.
            seen.clear()
            shot = Path(tmp) / "shot.png"
            shot.write_bytes(_PNG)
            s.COUNCIL = cfg.Council({"a": alpha, "c": blind}, "test")
            out = await s.ask_all("what breaks?", materials=[mm.Material(path=str(shot))])
            assert [m for m, _ in seen] == ["a"], seen
            assert "Cyclops" in out and "vision: false" in out, out

            s.COUNCIL = cfg.Council({"c": blind}, "test")
            out = await s.ask_all("what breaks?", materials=[mm.Material(path=str(shot))])
            assert "nobody left to show it to" in out, out

            # A material that cannot be read stops the call. The alternative is
            # a council answering fluently about a document it never received.
            out = await s.ask_all("what breaks?",
                                  materials=[mm.Material(path=str(Path(tmp) / "gone.md"))])
            assert "no such file" in out, out
    finally:
        s.COUNCIL, s.ask_member = saved
    print("ok  material (carried into every round; images skip the blind seats)")


async def test_revision_prompt_points_at_the_material() -> None:
    """The seat we cannot call is told what to open, not handed it back."""
    from model_council import materials as mm
    from model_council import server as s

    with tempfile.TemporaryDirectory() as tmp:
        spec = Path(tmp) / "spec.md"
        spec.write_text("the rule is x", encoding="utf-8")
        written = await s.revision_prompt(
            "what breaks?",
            answers=[s.PriorAnswer(model="a", text="the index"),
                     s.PriorAnswer(label="Subagent", text="the rollback")],
            seat="Subagent", round=1,
            materials=[mm.Material(label="Spec", path=str(spec)),
                       mm.Material(label="Note", text="an inline fragment")])

    # A file is named, at the position the members were given it: handing back
    # the whole document would cost the caller a copy to write out again, which
    # is the cost `materials` exists to remove.
    assert written.index("MATERIAL: Spec") < written.index("THE QUESTION"), written
    assert str(spec) in written and "the rule is x" not in written, written
    # Something with no file behind it has nowhere to point, so it goes in whole.
    assert "an inline fragment" in written, written
    print("ok  revision_prompt (material is named for the seat, not pasted back)")


def test_backups_are_the_same_seat() -> None:
    """A backup inherits the seat, keeps the seat's identity, and stays in order."""
    doc = {
        "timeout": 90,
        "providers": {
            "primary": {"base_url": "https://one/v1", "api_key": "k1", "format": "openai",
                        "proxy": False,
                        "headers": {"X-Token": "issued-for-one-host-only"}},
            "spare": {"base_url": "https://two/v1", "api_key": "k2", "format": "anthropic"},
        },
        "members": [
            {"id": "sol", "provider": "primary", "model": "gpt-5.6-sol",
             "label": "GPT-5.6-sol", "weight": 2, "temperature": 0.2, "retries": 0,
             "backups": [
                 {"provider": "spare"},
                 {"base_url": "https://three/v1", "api_key": "k3",
                  "model": "gpt-5-codex", "timeout": 30},
             ]},
            {"id": "plain", "provider": "primary", "model": "m"},
        ],
    }
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "config.json"
        path.write_text(json.dumps(doc), encoding="utf-8")
        with env(COUNCIL_CONFIG=str(path)):
            c = cfg.load_council()

    assert not c.warnings, c.warnings
    # Backups are not members: the roster is still the two seats that were listed,
    # and nothing can address a backup directly or count it as a second opinion.
    assert sorted(c.members) == ["plain", "sol"], sorted(c.members)
    assert c.members["plain"].backups == []
    assert c.members["plain"].sources == [c.members["plain"]]

    sol = c.members["sol"]
    one, two = sol.backups
    assert sol.sources == [sol, one, two]

    # The connection comes from the backup's own provider, never from the seat.
    assert (one.base_url, one.api_key, one.format) == ("https://two/v1", "k2", "anthropic")
    assert (two.base_url, two.api_key, two.format) == ("https://three/v1", "k3", "openai")

    # Everything that is not the connection falls down from the seat, so the
    # common case — the same model on another relay — is one line of config.
    assert one.model == "gpt-5.6-sol" and one.temperature == 0.2 and one.retries == 0
    assert one.timeout == 90                     # the seat's, from the file's default
    assert two.model == "gpt-5-codex"            # unless the backup says otherwise
    assert two.timeout == 30

    # headers do not: a header written for one endpoint is routinely a credential
    # for it, and a backup is by definition a different host.
    assert sol.headers == {"X-Token": "issued-for-one-host-only"}
    assert one.headers == {} and two.headers == {}, (one.headers, two.headers)

    # Nor does the route. The seat here goes direct because its own gateway is
    # somewhere a proxy cannot follow; handing that down would send the public
    # backups — the ones most likely to need the proxy — around it too.
    assert sol.proxy is False
    assert one.proxy is None and two.proxy is None, (one.proxy, two.proxy)

    # One seat, one voice: the label and weight the council reads are the seat's.
    assert (one.label, two.label) == ("GPT-5.6-sol", "GPT-5.6-sol")
    assert (one.weight, two.weight) == (2, 2)
    assert (one.id, two.id) == ("sol#1", "sol#2")   # only so a chain can be named
    print("ok  backups (inherit the seat, bring their own connection, keep its identity)")


def test_a_backup_cannot_become_a_second_member() -> None:
    """The ways a backup could quietly turn into something else are refused."""
    doc = {
        "providers": {
            "a": {"base_url": "https://a/v1", "api_key": "ka"},
            "b": {"base_url": "https://b/v1", "api_key": "kb"},
        },
        "members": [
            {"id": "renamed", "provider": "a", "model": "m",
             "backups": [{"provider": "b", "id": "other", "label": "Other", "weight": 9}]},
            {"id": "mixed", "provider": "a", "model": "m",
             "backups": [{"provider": "b", "api_key": "${SOMEWHERE_ELSE}"}]},
            {"id": "nested", "provider": "a", "model": "m",
             "backups": [{"provider": "b", "backups": [{"provider": "a"}]}]},
            {"id": "greedy", "provider": "a", "model": "m",
             "backups": [{"provider": "b"}] * 7},
            {"id": "halfway", "provider": "a", "model": "m",
             "backups": [{"base_url": "https://c/v1"}]},
        ],
    }
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "config.json"
        path.write_text(json.dumps(doc), encoding="utf-8")
        with env(COUNCIL_CONFIG=str(path)):
            c = cfg.load_council()

    def warned(*words: str) -> bool:
        return any(all(w in x for w in words) for x in c.warnings)

    # A backup that tried to name itself is still the seat, and is told so.
    renamed = c.members["renamed"].backups[0]
    assert (renamed.id, renamed.label, renamed.weight) == ("renamed#1", "renamed", 1.0)
    assert warned("renamed", "'id'", "'label'", "'weight'"), c.warnings

    # The atomic-connection rule holds one level down, for the same reason: half
    # of provider 'b' and half of somewhere else sends b's host a key it never
    # issued. A backup that does it is parked, not merged.
    parked = c.members["mixed"].backups[0]
    assert not parked.enabled and parked.disabled_reason
    assert c.members["mixed"].sources == [c.members["mixed"]], "a parked backup is not tried"
    assert warned("mixed", "api_key"), c.warnings

    # A chain is one list read top to bottom; nesting would hide that order.
    assert c.members["nested"].backups[0].backups == []
    assert warned("nested", "do not nest"), c.warnings

    assert len(c.members["greedy"].backups) == cfg._MAX_BACKUPS
    assert warned("greedy", "dropped"), c.warnings

    # An incomplete backup is kept and reported rather than silently dropped: it
    # is a seat that looks two-deep and is one, which is worth saying out loud.
    assert c.members["halfway"].backups[0].missing == ["api_key"]
    assert warned("halfway", "incomplete", "api_key"), c.warnings
    print("ok  a backup cannot rename itself, split a connection, nest, or pile up")


@contextmanager
def serving_hosts(**by_host):
    """Serve a different outcome per host, and record the hosts actually called.

    A chain is only testable if the two ends can be told apart, which is what
    dispatching on the host buys: the recorded list is the order the seat walked
    its connections in.
    """
    from model_council import providers as p

    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        host = request.url.host
        seen.append(host)
        return by_host[host]()

    saved = p._client
    p._client = lambda m, timeout=None: httpx.AsyncClient(
        transport=httpx.MockTransport(handler))
    try:
        yield seen
    finally:
        p._client = saved


def _seat(**kw) -> cfg.Member:
    """A seat on 'one', backed by 'two' and then 'three'."""
    def conn(i: str, host: str, model: str) -> cfg.Member:
        return cfg.Member(id=i, label="Sol", model=model, api_key="k",
                          base_url=f"https://{host}/v1", retry_backoff=0)
    return cfg.Member(id="sol", label="Sol", model="gpt-5.6-sol", api_key="k",
                      base_url="https://one/v1", retry_backoff=0,
                      backups=[conn("sol#1", "two", "gpt-5.6-sol"),
                               conn("sol#2", "three", "gpt-5-codex")],
                      **kw)


async def test_a_seat_falls_through_to_its_backups() -> None:
    """The next connection is tried when the one before it does not answer."""
    from model_council import server as srv
    from model_council.providers import ask_member

    # Nothing changes for a seat whose primary works: the backups are not called,
    # and the answer is not dressed up as having survived anything.
    with serving_hosts(one=_ok, two=_ok, three=_ok) as seen:
        a = await ask_member(_seat(), "hi")
    assert a.ok and seen == ["one"], (a, seen)
    assert a.backup_rank == 0 and a.source is a.member
    assert "backup" not in srv._labelled(a, False), srv._labelled(a, False)

    # A primary that is merely busy spends its own retries first — a backup is
    # for a connection that is out, not for one that is slow to say yes.
    with serving_hosts(one=_status(503), two=_ok, three=_ok) as seen:
        a = await ask_member(_seat(), "hi")
    assert a.ok and seen == ["one", "one", "one", "two"], seen
    assert a.backup_rank == 1 and a.source.base_url == "https://two/v1"

    # A revoked key is not retried and not fatal either: it is this way in being
    # shut, which is exactly what the next way in is for.
    with serving_hosts(one=_status(401), two=_ok, three=_ok) as seen:
        a = await ask_member(_seat(), "hi")
    assert a.ok and seen == ["one", "two"], seen

    # Which model actually spoke reaches the reader. The backup here carries a
    # different model id, and a council is a comparison of models.
    with serving_hosts(one=_status(401), two=_status(404), three=_ok) as seen:
        a = await ask_member(_seat(), "hi")
    assert a.ok and a.backup_rank == 2 and seen == ["one", "two", "three"], (a, seen)
    header = srv._labelled(a, False)
    assert "gpt-5-codex" in header and "backup 2" in header, header

    # retries=0 on the primary is the fail-fast shape: one attempt, then over.
    with serving_hosts(one=_status(503), two=_ok, three=_ok) as seen:
        a = await ask_member(_seat(retries=0), "hi")
    assert a.ok and seen == ["one", "two"], seen

    # A parked backup is not a connection.
    seat = _seat()
    seat.backups[0].enabled = False
    with serving_hosts(one=_status(401), two=_ok, three=_ok) as seen:
        a = await ask_member(seat, "hi")
    assert a.ok and seen == ["one", "three"], seen

    # `ask` hands back the answer and nothing else — there is no header to carry
    # the substitution, so it goes in front of the text or nowhere at all.
    saved = srv.COUNCIL
    try:
        srv.COUNCIL = cfg.Council({"sol": _seat()}, "test")
        with serving_hosts(one=_status(401), two=_ok, three=_ok):
            out = await srv.ask("sol", "hi")
        assert out.startswith("[answered by Sol's backup 1"), out
        assert "https://two/v1" in out and out.endswith("pong"), out

        with serving_hosts(one=_ok, two=_ok, three=_ok):
            assert await srv.ask("sol", "hi") == "pong", "a working primary says nothing"
    finally:
        srv.COUNCIL = saved
    print("ok  fallover (only when a connection is out, in order, and reported)")


async def test_a_seat_that_is_wholly_down_says_what_it_tried() -> None:
    """With every way in shut, no single failure is the story — so all of them are."""
    from model_council.providers import ask_member

    # Retries belong to the connection, not to the seat: each of the three gets
    # its own budget, so the chain is flattened here to make the order readable.
    seat = _seat(retries=0)
    for b in seat.backups:
        b.retries = 0
    with serving_hosts(one=_status(500), two=_status(401), three=_boom()) as seen:
        a = await ask_member(seat, "hi")
    assert not a.ok, a
    assert seen == ["one", "two", "three"], seen
    assert a.attempts == 3, a.attempts        # what the seat spent, across them all
    # Each line names the model and the host, because two connections for one
    # seat routinely differ in both and a reader has to know which was refused.
    assert "3 connections" in a.text, a.text
    for fragment in ("gpt-5.6-sol at https://one/v1", "HTTP 500",
                     "gpt-5.6-sol at https://two/v1", "HTTP 401",
                     "gpt-5-codex at https://three/v1", "ConnectError"):
        assert fragment in a.text, (fragment, a.text)

    # A seat with no backups keeps the message it always had: a chain of one
    # reported as a chain would be ceremony around a single sentence.
    plain = cfg.Member(id="x", label="X", model="m", base_url="https://one/v1",
                       api_key="k", retry_backoff=0, retries=0)
    with serving_hosts(one=_status(500)):
        a = await ask_member(plain, "hi")
    assert not a.ok and a.text.startswith("[X HTTP 500"), a.text
    assert "connections" not in a.text, a.text

    # An incomplete connection is skipped rather than called, and the skip is
    # reported alongside the failures — a seat that looks two-deep and is one.
    half = cfg.Member(id="y", label="Y", model="m", base_url="https://one/v1",
                      api_key="k", retry_backoff=0, retries=0,
                      backups=[cfg.Member(id="y#1", label="Y", model="m",
                                          base_url="https://two/v1")])
    with serving_hosts(one=_status(500)) as seen:
        a = await ask_member(half, "hi")
    assert seen == ["one"], "an unconfigured backup must not be dialled"
    assert "not configured — missing api_key" in a.text, a.text
    print("ok  a seat with nothing left reports every connection it tried")


def main() -> None:
    test_default_roster()
    test_env_roster()
    test_file_config()
    test_connection_is_atomic()
    test_proxy_policy()
    test_proxy_defaults_and_overrides()
    test_a_proxy_that_cannot_be_used_is_caught_at_load()
    asyncio.run(test_the_route_is_visible())
    test_retry_settings()
    test_weights()
    test_backups_are_the_same_seat()
    test_a_backup_cannot_become_a_second_member()
    test_registry_metadata_agrees()
    test_explicit_beats_discovered()
    test_shipped_example_is_valid()
    test_missing_file_falls_back()
    test_allowlist_parsing()
    test_http_refuses_to_serve_the_world()
    asyncio.run(test_http_gate())
    asyncio.run(test_retry_transport())
    asyncio.run(test_a_seat_falls_through_to_its_backups())
    asyncio.run(test_a_seat_that_is_wholly_down_says_what_it_tried())
    test_material_loading()
    asyncio.run(test_material_reaches_the_wire())
    asyncio.run(test_truncation_is_not_silent())
    asyncio.run(test_material_travels_every_round())
    asyncio.run(test_revision_prompt_points_at_the_material())
    asyncio.run(test_rounds_carry_the_previous_answers())
    asyncio.run(test_the_ceiling_is_on_the_call_not_the_discussion())
    asyncio.run(test_weights_reach_the_reader_not_the_members())
    asyncio.run(test_guests_join_the_argument())
    asyncio.run(test_revise_drives_a_round_from_outside())
    asyncio.run(test_revision_prompt_writes_for_the_seat_we_cannot_ask())
    asyncio.run(test_members_argue_with_letters_not_brands())
    asyncio.run(test_tools())


if __name__ == "__main__":
    main()
