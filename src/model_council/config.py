"""Who sits on the council, and where their configuration comes from.

The abstraction has two layers:

  provider — an endpoint: base_url + api_key + which wire format it speaks
  member   — one model on some provider, addressed by a short id

One provider can host many members (a relay that exposes several models), and a
member may inline its own connection details instead of naming a provider.

A member with `format: "sampling"` has no endpoint at all: it is answered by the
MCP client's own model, back down the session that is already open.

Configuration arrives from whichever of these is most explicit:

  1. the JSON file COUNCIL_CONFIG points at
  2. environment variables, if COUNCIL_MODELS names a roster
  3. a JSON file found at the default path
  4. the built-in default roster, read from environment variables

The chosen source is reported by the `list_council` tool, so it is never a
mystery which one won.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_CONFIG_PATH = Path.home() / ".config" / "model-council" / "config.json"

# Roster used when COUNCIL_MODELS is unset — the two providers this project
# started with. Their env prefixes (CHATGPT_*, GLM_*) keep working unchanged.
LEGACY_DEFAULTS: dict[str, dict] = {
    "chatgpt": {"label": "ChatGPT", "model": "gpt-5-codex"},
    "glm": {"label": "GLM", "model": "glm-4.6",
            "base_url": "https://open.bigmodel.cn/api/paas/v4"},
}

_ENV_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")

# Connection fields a member may inherit from its provider.
_PROVIDER_FIELDS = {"base_url", "api_key", "format", "headers", "timeout", "proxy",
                    "retries", "retry_backoff"}
_MEMBER_FIELDS = _PROVIDER_FIELDS | {
    "id", "model", "label", "max_tokens", "temperature", "enabled", "weight",
}

# A ceiling on retries, because the cost of a typo here is paid in real requests
# to a real provider: `"retries": 50` should not turn one question into fifty.
_MAX_RETRIES = 5

# Weight is deliberately not a provider field: two members on one relay can be a
# frontier model and a small fast one, so it belongs to the seat, not the
# endpoint. Only ratios mean anything, and the ceiling keeps one member from
# being given a number so large that every other answer rounds to noise.
_MAX_WEIGHT = 10.0

# The third way to reach a model, alongside the two HTTP formats: don't. A
# member with this format is answered by the MCP client's own model, over the
# session it already has open, via a `sampling/createMessage` request back down
# the connection. It needs no endpoint, no key, and nothing to pay for.
SAMPLING = "sampling"

# Who you talk to, and how you prove who you are. These three travel together:
# a member that names a provider may not override any of them, because taking
# the key from one endpoint and the URL from another sends that key to a host it
# was never issued for. A member with no provider supplies all three itself, so
# nothing can be mixed. `headers` and `timeout` are not part of this identity
# and stay overridable per member, and neither is `proxy`: it changes the route
# to the host, not which host or which credential, and over TLS a proxy sees
# only a CONNECT tunnel.
_ATOMIC_CONNECTION = {"base_url", "api_key", "format"}


# --------------------------------------------------------------------------- #
# Minimal .env loader (no external dependency).
#
# A convenience for local development only: when an MCP client launches the
# server, configuration comes from the client's `env` block and the working
# directory is not the repo. Set COUNCIL_ENV_FILE to point at a specific file.
# --------------------------------------------------------------------------- #
def load_dotenv(path: Path | None = None) -> None:
    path = path or Path(os.environ.get("COUNCIL_ENV_FILE", ".env")).expanduser()
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))


# --------------------------------------------------------------------------- #
# Model
# --------------------------------------------------------------------------- #
def _clamp_weight(value) -> float:
    """Read a weight, refusing the values that would break the comparison.

    A negative weight, a NaN, or a word where a number belongs is not a weaker
    opinion — it is a typo, and one that would silently corrupt every ranking
    the weights are read for. Falling back to the neutral 1 keeps the member in
    the discussion on equal footing, and `list_council` prints the effective
    value, so the typo is one command away from being visible.
    """
    try:
        w = float(value)
    except (TypeError, ValueError):
        return 1.0
    if w != w:                      # NaN: every comparison against it is false
        return 1.0
    return max(0.0, min(w, _MAX_WEIGHT))


@dataclass
class Member:
    """One seat on the council."""

    id: str
    model: str = ""
    base_url: str = ""
    api_key: str = ""
    format: str = "openai"
    label: str = ""
    max_tokens: int = 8192
    temperature: float | None = None
    # How much this council trusts this member relative to the others. It
    # changes nothing about the call: it travels with the answer so that
    # whoever reads a disagreement knows which side the roster rates higher.
    weight: float = 1.0
    headers: dict[str, str] = field(default_factory=dict)
    timeout: float = 180.0
    # Extra attempts a transient failure gets — a 429, a 5xx, a dropped
    # connection — and the first backoff step in seconds, doubling from there.
    # 0 restores the old behaviour of reporting the first failure as final.
    retries: int = 2
    retry_backoff: float = 1.0
    # None = follow the environment's proxy settings (the default);
    # False = connect directly, ignoring them; a URL = use that proxy.
    proxy: str | bool | None = None
    enabled: bool = True
    disabled_reason: str = ""

    def __post_init__(self) -> None:
        self.label = self.label or self.id
        self.base_url = (self.base_url or "").rstrip("/")
        self.format = (self.format or "openai").lower()
        self.retries = max(0, min(int(self.retries), _MAX_RETRIES))
        self.retry_backoff = max(0.0, float(self.retry_backoff))
        self.weight = _clamp_weight(self.weight)

    @property
    def configured(self) -> bool:
        return not self.missing

    @property
    def is_sampling(self) -> bool:
        return self.format == SAMPLING

    @property
    def missing(self) -> list[str]:
        # A sampling member rides the MCP session that is already open, so there
        # is no endpoint to point at and no key to present. `model` is only ever
        # a hint to the client, which is free to ignore it.
        if self.is_sampling:
            return []
        return [f for f in ("base_url", "api_key", "model") if not getattr(self, f)]


@dataclass
class Council:
    members: dict[str, Member]
    source: str
    warnings: list[str] = field(default_factory=list)

    @property
    def ids(self) -> list[str]:
        return [m.id for m in self.members.values() if m.enabled]

    def get(self, member_id: str) -> Member | None:
        m = self.members.get(member_id)
        return m if m and m.enabled else None

    def resolve(self, ids: list[str] | None) -> tuple[list[Member], list[str]]:
        """Turn a requested id list (or None = everyone) into members + unknowns."""
        if ids is None:
            return [m for m in self.members.values() if m.enabled], []
        found, unknown = [], []
        for i in ids:
            m = self.get(i)
            found.append(m) if m else unknown.append(i)
        return found, unknown


# --------------------------------------------------------------------------- #
# Loading — environment variables
# --------------------------------------------------------------------------- #
def _prefix(member_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "_", member_id).upper()


def _env_json(name: str) -> dict:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return {}
    try:
        val = json.loads(raw)
        return val if isinstance(val, dict) else {}
    except json.JSONDecodeError:
        return {}


_DIRECT = {"false", "0", "no", "off", "none", "direct"}


def _env_proxy(prefix: str) -> str | bool | None:
    """`<ID>_PROXY`: unset = follow the environment, a falsey word = go direct,
    anything else = the proxy URL to use."""
    raw = os.environ.get(f"{prefix}_PROXY", "").strip()
    if not raw:
        return None
    return False if raw.lower() in _DIRECT else raw


def _defaults(data: dict | None = None) -> dict:
    """Council-wide fallbacks for the per-call knobs.

    A config file's top level if it sets them, else the environment, else the
    built-in values. Each member may still override any of them.
    """
    data = data or {}
    return {
        "timeout": float(data.get("timeout", os.environ.get("COUNCIL_TIMEOUT", "180"))),
        "retries": int(data.get("retries", os.environ.get("COUNCIL_RETRIES", "2"))),
        "retry_backoff": float(
            data.get("retry_backoff", os.environ.get("COUNCIL_RETRY_BACKOFF", "1"))),
    }


def _member_from_env(member_id: str, defaults: dict, fallback: dict) -> Member:
    p = _prefix(member_id)
    temp = os.environ.get(f"{p}_TEMPERATURE", "").strip()
    return Member(
        id=member_id,
        model=os.environ.get(f"{p}_MODEL", defaults.get("model", "")),
        base_url=os.environ.get(f"{p}_BASE_URL", defaults.get("base_url", "")),
        api_key=os.environ.get(f"{p}_API_KEY", ""),
        format=os.environ.get(f"{p}_FORMAT", defaults.get("format", "openai")),
        label=os.environ.get(f"{p}_LABEL", defaults.get("label", "")),
        max_tokens=int(os.environ.get(f"{p}_MAX_TOKENS", "8192")),
        temperature=float(temp) if temp else None,
        weight=_clamp_weight(os.environ.get(f"{p}_WEIGHT", "1")),
        headers=_env_json(f"{p}_HEADERS"),
        timeout=float(os.environ.get(f"{p}_TIMEOUT", str(fallback["timeout"]))),
        retries=int(os.environ.get(f"{p}_RETRIES", str(fallback["retries"]))),
        retry_backoff=float(
            os.environ.get(f"{p}_RETRY_BACKOFF", str(fallback["retry_backoff"]))),
        proxy=_env_proxy(p),
        enabled=os.environ.get(f"{p}_ENABLED", "1").lower() not in ("0", "false", "no"),
    )


def _from_env() -> Council:
    fallback = _defaults()
    listed = [s.strip() for s in os.environ.get("COUNCIL_MODELS", "").split(",") if s.strip()]
    ids = listed or list(LEGACY_DEFAULTS)
    members = {
        i: _member_from_env(i, LEGACY_DEFAULTS.get(i, {}) if not listed else {}, fallback)
        for i in ids
    }
    source = ("environment (COUNCIL_MODELS)" if listed
              else "environment (default roster: %s)" % ", ".join(ids))
    return Council(members, source)


# --------------------------------------------------------------------------- #
# Loading — JSON file
# --------------------------------------------------------------------------- #
def _expand(value):
    """Substitute ${ENV_VAR} references anywhere in the config."""
    if isinstance(value, str):
        return _ENV_REF.sub(lambda m: os.environ.get(m.group(1), ""), value)
    if isinstance(value, dict):
        return {k: _expand(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand(v) for v in value]
    return value


def _uncommented(raw: dict) -> dict:
    """JSON has no comment syntax, so treat $-prefixed keys as annotations."""
    return {k: v for k, v in raw.items() if not k.startswith("$")}


def _from_file(path: Path) -> Council:
    warnings: list[str] = []
    data = _expand(json.loads(path.read_text(encoding="utf-8")))
    fallback = _defaults(data)

    providers: dict[str, dict] = {}
    for name, raw in (data.get("providers") or {}).items():
        raw = _uncommented(raw)
        unknown = set(raw) - _PROVIDER_FIELDS
        if unknown:
            warnings.append(f"provider '{name}': ignored unknown field(s) {sorted(unknown)}")
        providers[name] = {k: v for k, v in raw.items() if k in _PROVIDER_FIELDS}

    members: dict[str, Member] = {}
    for raw in data.get("members") or []:
        raw = _uncommented(raw)
        member_id = raw.get("id")
        if not member_id:
            warnings.append("skipped a member with no 'id'")
            continue
        provider_name = raw.get("provider")
        if provider_name and provider_name not in providers:
            warnings.append(f"member '{member_id}': unknown provider '{provider_name}'")
        unknown = set(raw) - _MEMBER_FIELDS - {"provider"}
        if unknown:
            warnings.append(f"member '{member_id}': ignored unknown field(s) {sorted(unknown)}")

        # A member that names a provider must take the whole connection from it.
        # Refuse rather than merge: a half-overridden connection would quietly
        # send one endpoint's credentials to another endpoint's host.
        mixed = sorted(set(raw) & _ATOMIC_CONNECTION) if provider_name else []
        if mixed:
            warnings.append(
                f"member '{member_id}': disabled — it uses provider '{provider_name}' but also "
                f"sets {mixed}. base_url, api_key and format are one unit; overriding part of "
                f"them would send '{provider_name}' credentials somewhere they were not issued "
                f"for. Either drop {mixed}, or remove 'provider' and give this member its own "
                f"complete connection."
            )
            base: dict = {k: v for k, v in raw.items()
                          if k in _MEMBER_FIELDS and k not in _PROVIDER_FIELDS}
            base["enabled"] = False
            base["disabled_reason"] = "mixes a provider's connection with its own"
        else:
            base = dict(providers.get(provider_name, {})) if provider_name else {}
            for k, v in fallback.items():
                base.setdefault(k, v)
            base.update({k: v for k, v in raw.items() if k in _MEMBER_FIELDS})

        if member_id in members:
            warnings.append(f"duplicate member id '{member_id}' — later one wins")
        members[member_id] = Member(**base)

    return Council(members, f"file: {path}", warnings)


def _config_path() -> tuple[Path | None, list[str]]:
    """Which source wins, most explicit first.

    A pointer to a file beats a roster in the environment, which beats a file
    the server merely happened to find, which beats the built-in default. The
    middle rule matters: a client that passes COUNCIL_MODELS means it, and
    quietly preferring a leftover ~/.config file over what the client just
    supplied is impossible to diagnose from the outside — you fill in a form,
    nothing you typed takes effect, and nothing says why.
    """
    explicit = os.environ.get("COUNCIL_CONFIG", "").strip()
    if explicit:
        p = Path(explicit).expanduser()
        if p.exists():
            return p, []
        return None, [f"COUNCIL_CONFIG points at {p}, which does not exist "
                      f"— fell back to environment variables"]
    if os.environ.get("COUNCIL_MODELS", "").strip():
        return None, []
    if DEFAULT_CONFIG_PATH.exists():
        return DEFAULT_CONFIG_PATH, []
    return None, []


def load_council() -> Council:
    load_dotenv()
    path, warnings = _config_path()
    if path:
        try:
            council = _from_file(path)
            council.warnings = warnings + council.warnings
            return council
        except Exception as e:  # noqa: BLE001 - a bad file must not stop the server
            warnings.append(f"could not read {path} ({type(e).__name__}: {e}) "
                            f"— fell back to environment variables")
    council = _from_env()
    council.warnings = warnings + council.warnings
    return council
