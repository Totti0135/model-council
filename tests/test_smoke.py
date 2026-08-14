#!/usr/bin/env python3
"""Smoke test — no pytest required, run it directly:

    python tests/test_smoke.py

Verifies the tool surface, both configuration sources, provider inheritance and
${ENV} expansion, and graceful degradation when a member is unconfigured. If a
usable .env is present it finishes with a live round-trip through ask_all.
"""
from __future__ import annotations

import asyncio
import json
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path

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
            ("_BASE_URL", "_API_KEY", "_MODEL", "_FORMAT", "_HEADERS", "_ENABLED")
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


def test_registry_metadata_agrees() -> None:
    """server.json must name the same server the README claims ownership of.

    The registry verifies ownership by finding `mcp-name: <name>` in the README
    that PyPI shows; if the two ever disagree, publishing fails after the PyPI
    upload has already happened, which is the worst moment to find out.
    """
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


def main() -> None:
    test_default_roster()
    test_env_roster()
    test_file_config()
    test_connection_is_atomic()
    test_proxy_policy()
    test_registry_metadata_agrees()
    test_explicit_beats_discovered()
    test_shipped_example_is_valid()
    test_missing_file_falls_back()
    asyncio.run(test_tools())


if __name__ == "__main__":
    main()
