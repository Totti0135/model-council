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
        if k.startswith("COUNCIL_") or k.endswith(
            ("_BASE_URL", "_API_KEY", "_MODEL", "_FORMAT", "_HEADERS", "_ENABLED",
             "_WEIGHT")
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

    async def answering(m, prompt, system=None):
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

    async def answering(m, prompt, system=None):
        seen.append((m.id, prompt))
        n = sum(1 for i, _ in seen if i == m.id)
        return Answer(m, f"{m.label} says {n}", ok=True)

    async def only_alpha_answers(m, prompt, system=None):
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

    async def answering(m, prompt, system=None):
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
        assert "Subagent" not in r2.split("[Subagent speaks once")[0], \
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
             KIMI_KEY="sk-k", INTERNAL_KEY="sk-i"):
        c = cfg.load_council()
    assert not c.warnings, c.warnings
    assert c.ids == ["gpt5", "codex", "glm", "kimi", "inhouse"], c.ids  # 'spare' is disabled
    assert all(c.members[i].configured for i in c.ids), c.members
    assert c.members["inhouse"].proxy is False        # inherited from its provider
    assert c.members["gpt5"].proxy is None            # everyone else follows the env
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
    assert names == ["ask", "ask_all", "list_council", "probe_models"], names

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


def main() -> None:
    test_default_roster()
    test_env_roster()
    test_file_config()
    test_connection_is_atomic()
    test_proxy_policy()
    test_retry_settings()
    test_weights()
    test_registry_metadata_agrees()
    test_explicit_beats_discovered()
    test_shipped_example_is_valid()
    test_missing_file_falls_back()
    test_allowlist_parsing()
    test_http_refuses_to_serve_the_world()
    asyncio.run(test_http_gate())
    asyncio.run(test_retry_transport())
    asyncio.run(test_rounds_carry_the_previous_answers())
    asyncio.run(test_weights_reach_the_reader_not_the_members())
    asyncio.run(test_guests_join_the_argument())
    asyncio.run(test_tools())


if __name__ == "__main__":
    main()
