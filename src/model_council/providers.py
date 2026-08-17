"""Wire-format transports.

Two shapes cover nearly every endpoint worth talking to: the OpenAI Chat
Completions format (`POST {base_url}/chat/completions`) and the Anthropic
Messages format (`POST {base_url}/v1/messages`).

A call that fails for a transient reason is retried; one that fails for a
permanent reason is not. What survives that is returned as readable text rather
than raised, so one unreachable member degrades the answer instead of killing
the whole call.
"""
from __future__ import annotations

import asyncio
import os
import random
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import httpx

from model_council.config import Member

_PROXY_VARS = ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY")

# Failures worth another attempt: the endpoint is busy, rate-limited, or briefly
# broken, and the same request may well succeed in a moment. Everything else —
# 400, 401, 403, 404 — is a fact about the request, so asking again only spends
# the same quota to be told the same thing.
_RETRYABLE_STATUS = frozenset({408, 409, 425, 429, 500, 502, 503, 504, 529})

# The longest we will wait between attempts. A member rate-limited for longer
# than this is not worth sitting on inside a tool call: give up and report what
# the endpoint asked for, so the caller can decide whether to come back.
_MAX_BACKOFF = 30.0


@dataclass
class Answer:
    """One member's reply — and whether it is a reply at all.

    `ask_member` never raises: a failure comes back as readable text so one dead
    member degrades the result instead of killing the call. `ok` is what keeps
    that text distinguishable from a real answer, which starts to matter once
    answers are fed back into a later round — no model should be handed another
    model's error message to critique.
    """

    member: Member
    text: str
    ok: bool
    attempts: int = 1

    def __str__(self) -> str:
        return self.text


class BadPayload(Exception):
    """The endpoint answered, but not with an answer."""


def _client(m: Member, timeout: float | None = None) -> httpx.AsyncClient:
    """A client following this member's proxy policy.

    Left alone, httpx reads HTTP_PROXY/HTTPS_PROXY from the environment. That is
    the right default, but it silently breaks members on a network the proxy
    cannot reach — an internal gateway behind a VPN fails with a bare
    ConnectError that says nothing about a proxy being involved. `proxy: false`
    is the escape hatch; a URL routes this member through a specific proxy.
    """
    kwargs: dict = {"timeout": m.timeout if timeout is None else timeout}
    if m.proxy is False:
        kwargs["trust_env"] = False
    elif isinstance(m.proxy, str) and m.proxy:
        kwargs["proxy"] = m.proxy
    return httpx.AsyncClient(**kwargs)


def _proxy_hint(m: Member) -> str:
    """A connection failure through an inherited proxy looks like a plain
    ConnectError, which says nothing about the proxy. Say it instead."""
    if m.proxy is not None or not any(os.environ.get(v) for v in _PROXY_VARS):
        return ""
    return (" — note a proxy is set in this server's environment and this member "
            "follows it; if the endpoint is not reachable through that proxy "
            '(an internal host, typically), give the member `"proxy": false`')


async def _call_openai(m: Member, prompt: str, system: str | None) -> str:
    messages: list[dict] = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    payload: dict = {"model": m.model, "messages": messages}
    if m.temperature is not None:
        payload["temperature"] = m.temperature
    headers = {
        "Authorization": f"Bearer {m.api_key}",
        "Content-Type": "application/json",
        **m.headers,
    }
    async with _client(m) as client:
        r = await client.post(m.base_url + "/chat/completions", json=payload, headers=headers)
        r.raise_for_status()
        data = r.json()
    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        raise BadPayload(f"unexpected response shape: {str(data)[:600]}") from None
    if not content:
        raise BadPayload("empty response")
    return content


async def _call_anthropic(m: Member, prompt: str, system: str | None) -> str:
    # max_tokens is required by this format, which is why it only applies here.
    payload: dict = {
        "model": m.model,
        "max_tokens": m.max_tokens,
        "messages": [{"role": "user", "content": prompt}],
    }
    if system:
        payload["system"] = system
    if m.temperature is not None:
        payload["temperature"] = m.temperature
    headers = {
        "x-api-key": m.api_key,
        "anthropic-version": "2023-06-01",
        "Content-Type": "application/json",
        **m.headers,
    }
    async with _client(m) as client:
        r = await client.post(m.base_url + "/v1/messages", json=payload, headers=headers)
        r.raise_for_status()
        data = r.json()
    text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
    if not text:
        raise BadPayload(f"unexpected response shape: {str(data)[:600]}")
    return text


# --------------------------------------------------------------------------- #
# Retry
# --------------------------------------------------------------------------- #
def _retry_after(response: httpx.Response) -> float | None:
    """How long the endpoint itself asked us to wait, if it said.

    A provider that publishes its own pacing knows better than any backoff
    curve we could guess, so it wins when present. Both legal forms are
    accepted: a number of seconds, or an HTTP-date.
    """
    raw = response.headers.get("retry-after", "").strip()
    if not raw:
        return None
    try:
        return max(0.0, float(raw))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return max(0.0, (when - datetime.now(timezone.utc)).total_seconds())


def _backoff(attempt: int, base: float) -> float:
    """Exponential, with half the window jittered.

    Without the jitter, members that failed together — the usual case, since
    ask_all fires them in parallel — would wake together and hit the endpoint in
    the same instant again.
    """
    window = min(base * 2 ** (attempt - 1), _MAX_BACKOFF)
    return window / 2 + random.uniform(0, window / 2)


async def ask_member(m: Member, prompt: str, system: str | None = None) -> Answer:
    """Send one prompt to one member, retrying transient failures, never raising."""
    if not m.configured:
        return Answer(m, f"[{m.label} is not configured — missing {', '.join(m.missing)}. "
                         f"See the server's README for how to configure a council member.]",
                      ok=False, attempts=0)

    call = _call_anthropic if m.format == "anthropic" else _call_openai
    attempts = m.retries + 1
    attempt = 0
    detail = ""
    while attempt < attempts:
        attempt += 1
        wait: float | None = None
        try:
            return Answer(m, await call(m, prompt, system), ok=True, attempts=attempt)
        except httpx.HTTPStatusError as e:
            detail = f"HTTP {e.response.status_code}: {e.response.text[:500]}"
            if e.response.status_code not in _RETRYABLE_STATUS:
                break
            wait = _retry_after(e.response)
            if wait is not None and wait > _MAX_BACKOFF:
                detail += (f" — the endpoint asked for {wait:.0f}s before the next attempt, "
                           f"longer than one call will wait; try again later")
                break
        except httpx.RequestError as e:
            detail = f"network error: {type(e).__name__}: {e}{_proxy_hint(m)}"
        except BadPayload as e:
            # It answered, so the connection and the credentials are fine; the
            # body is just not usable. Repeating the request would most likely
            # reproduce it, and pay for it again.
            detail = str(e)
            break
        except Exception as e:  # noqa: BLE001 - surface any failure back to the caller
            detail = f"error: {type(e).__name__}: {e}"
            break
        if attempt < attempts:
            await asyncio.sleep(_backoff(attempt, m.retry_backoff) if wait is None else wait)

    tried = f" (gave up after {attempt} attempts)" if attempt > 1 else ""
    return Answer(m, f"[{m.label} {detail}{tried}]", ok=False, attempts=attempt)


async def probe_member(m: Member) -> str:
    """Ask one member's endpoint which model ids it exposes.

    Deliberately single-shot: this is a diagnostic, and a diagnostic that
    retries just delays the diagnosis.
    """
    if m.format == "anthropic":
        url = m.base_url + "/v1/models"
        headers = {"x-api-key": m.api_key, "anthropic-version": "2023-06-01", **m.headers}
    else:
        url = m.base_url + "/models"
        headers = {"Authorization": f"Bearer {m.api_key}", **m.headers}
    async with _client(m, timeout=30) as client:
        r = await client.get(url, headers=headers)
        r.raise_for_status()
        data = r.json()
    ids = [x.get("id", "?") for x in data.get("data", [])]
    return ", ".join(ids[:60]) if ids else str(data)[:400]
