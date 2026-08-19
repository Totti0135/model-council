"""Wire-format transports.

Two shapes cover nearly every endpoint worth talking to: the OpenAI Chat
Completions format (`POST {base_url}/chat/completions`) and the Anthropic
Messages format (`POST {base_url}/v1/messages`).

A call that fails for a transient reason is retried; one that fails for a
permanent reason is not. A seat that lists backups then tries the next of them,
and so on down its chain. What survives all of that is returned as readable text
rather than raised, so one unreachable member degrades the answer instead of
killing the whole call.
"""
from __future__ import annotations

import asyncio
import os
import random
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import httpx

from model_council.config import Member, redact_proxy
from model_council.materials import Loaded, heading

_PROXY_VARS = ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY")


def env_proxy() -> tuple[str, str]:
    """The proxy this server's own environment applies, and which variable set
    it — ("", "") when there is none. A member that names no route of its own
    takes this one, so it belongs in `list_council` and in any error about a
    connection that never arrived."""
    for var in _PROXY_VARS:
        val = os.environ.get(var, "").strip()
        if val:
            return var, val
    return "", ""


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
    # The endpoint stopped this answer at a token ceiling rather than at its
    # end. The text is real and worth reading, so `ok` stays true; this is what
    # lets a caller notice that a member was cut off without parsing the note.
    truncated: bool = False
    # Which of the seat's connections produced this. The seat itself when it
    # answered on its primary, one of its backups when it did not, and None for
    # a guest, which was never called at all.
    source: Member | None = None
    # The bare failure reason, without the label wrapped around it. `text` is
    # what a reader sees; this is what a chain report is built out of.
    detail: str = ""
    # The connections that were tried, or skipped, before this answer — one line
    # each, in the order the chain was walked. Empty on the ordinary path.
    passed_over: list[str] = field(default_factory=list)

    def __str__(self) -> str:
        return self.text

    @property
    def backup_rank(self) -> int:
        """Which of the seat's backups answered, counting from 1.

        0 when the primary answered, which is also the answer for every seat
        that has no backups — so a caller can read this without first asking
        whether the feature is in use.
        """
        if self.source is None or self.source is self.member:
            return 0
        for n, b in enumerate(self.member.backups, start=1):
            if b is self.source:
                return n
        return 0


class BadPayload(Exception):
    """The endpoint answered, but not with an answer."""


def _client(m: Member, timeout: float | None = None) -> httpx.AsyncClient:
    """A client following this member's proxy policy.

    Left alone, httpx reads HTTP_PROXY/HTTPS_PROXY from the environment. That is
    the right default, but it silently breaks members on a network the proxy
    cannot reach — an internal gateway behind a VPN fails with a bare
    ConnectError that says nothing about a proxy being involved. `proxy: false`
    is the escape hatch; a URL routes this member through a specific proxy.

    The choice is per member on purpose: a council usually spans hosts that do
    not share a route — a public API that needs the proxy, an internal gateway
    the proxy cannot see — and the setting that fixes one breaks the other.
    """
    kwargs: dict = {"timeout": m.timeout if timeout is None else timeout}
    if m.proxy is False:
        kwargs["trust_env"] = False
    elif isinstance(m.proxy, str) and m.proxy:
        kwargs["proxy"] = m.proxy
    return httpx.AsyncClient(**kwargs)


def _proxy_hint(m: Member) -> str:
    """A connection failure says nothing about the route it took, and the route
    is the part that differs between members. Name it.

    Each of the three policies fails in a way that looks identical from the
    outside — a bare ConnectError — and has a different fix, so the hint says
    which one this member was on rather than leaving the reader to check the
    config against the environment.
    """
    if isinstance(m.proxy, str) and m.proxy:
        return (f" — this member is routed through {redact_proxy(m.proxy)}, and a proxy "
                f"that is down or cannot reach the endpoint fails exactly like the "
                f"endpoint being down")
    var, val = env_proxy()
    if not var:
        return ""
    if m.proxy is False:
        return (f" — this member is configured `proxy: false`, so it ignored "
                f"{var} and connected directly")
    return (f" — note {var}={redact_proxy(val)} is set in this server's environment "
            f"and this member follows it; if the endpoint is not reachable through "
            f'that proxy (an internal host, typically), give the member `"proxy": false`')


def _where(s: Member) -> str:
    """One connection, named the way a reader can act on it.

    The model belongs in it as much as the host does: two connections for one
    seat routinely carry the same model under different ids, and a chain report
    that named only hosts would not show which id was actually refused.
    """
    return f"{s.model or '(no model)'} at {s.base_url or '(no endpoint)'}"


# --------------------------------------------------------------------------- #
# What one request carries
#
# Material goes ahead of the question, in the order it was given, and the
# question goes last. Two reasons, and neither is presentation. Every member and
# every round then share a byte-identical prefix, which is the only shape an
# endpoint's cache can match; and a model reads the question knowing what it is
# about, rather than being asked to hold a question in mind through forty pages.
#
# With no material the content stays the bare string it has always been, so
# nothing changes for a council that never passes any.
# --------------------------------------------------------------------------- #
def _openai_content(prompt: str, docs: list[Loaded]) -> str | list[dict]:
    if not docs:
        return prompt
    parts: list[dict] = []
    for d in docs:
        if d.is_image:
            parts.append({"type": "text", "text": heading(d)})
            parts.append({"type": "image_url",
                          "image_url": {"url": f"data:{d.media_type};base64,{d.data}"}})
        else:
            parts.append({"type": "text", "text": f"{heading(d)}\n{d.text}"})
    parts.append({"type": "text", "text": prompt})
    return parts


def _anthropic_content(prompt: str, docs: list[Loaded], cache: bool) -> str | list[dict]:
    if not docs:
        return prompt
    blocks: list[dict] = []
    for d in docs:
        if d.is_image:
            blocks.append({"type": "text", "text": heading(d)})
            blocks.append({"type": "image",
                           "source": {"type": "base64", "media_type": d.media_type,
                                      "data": d.data}})
        else:
            blocks.append({"type": "text", "text": f"{heading(d)}\n{d.text}"})
    # One breakpoint, at the end of the material: everything before it is the
    # same in every call of a discussion, and everything after it is not. This
    # format has to be told where the reusable part stops; the OpenAI one has no
    # equivalent field, and gateways that cache do it by prefix on their own.
    if cache:
        blocks[-1]["cache_control"] = {"type": "ephemeral"}
    blocks.append({"type": "text", "text": prompt})
    return blocks


async def _call_openai(m: Member, prompt: str, system: str | None,
                       docs: list[Loaded]) -> tuple[str, str]:
    messages: list[dict] = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": _openai_content(prompt, docs)})
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
        choice = data["choices"][0]
        content = choice["message"]["content"]
    except (KeyError, IndexError, TypeError):
        raise BadPayload(f"unexpected response shape: {str(data)[:600]}") from None
    if not content:
        raise BadPayload("empty response")
    cut = ("the endpoint stopped this answer at its own length limit"
           if choice.get("finish_reason") == "length" else "")
    return content, cut


async def _call_anthropic(m: Member, prompt: str, system: str | None,
                          docs: list[Loaded]) -> tuple[str, str]:
    # max_tokens is required by this format, which is why it only applies here.
    payload: dict = {
        "model": m.model,
        "max_tokens": m.max_tokens,
        "messages": [{"role": "user",
                      "content": _anthropic_content(prompt, docs, m.cache)}],
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
    cut = (f"this member's max_tokens is {m.max_tokens}, and the answer reached it"
           if data.get("stop_reason") == "max_tokens" else "")
    return text, cut


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


async def _ask_one(m: Member, prompt: str, system: str | None,
                   docs: list[Loaded]) -> Answer:
    """One prompt down exactly one connection, retrying transient failures.

    Knows nothing about backups: it is handed a connection and reports what
    came back. `ask_member` is what decides whether a failure here is the end
    of the seat or the cue to try the next way in.
    """
    call = _call_anthropic if m.format == "anthropic" else _call_openai
    attempts = m.retries + 1
    attempt = 0
    detail = ""
    while attempt < attempts:
        attempt += 1
        wait: float | None = None
        try:
            text, cut = await call(m, prompt, system, docs)
            # An answer that stopped at a token ceiling reads as a finished one:
            # it argues, then simply ends. Saying so is the difference between a
            # position and a sentence that was interrupted — and it travels with
            # the text into the next round, where another member is about to
            # weigh it.
            if cut:
                text += f"\n\n[cut off — {cut}, so this answer is incomplete]"
            return Answer(m, text, ok=True, attempts=attempt, truncated=bool(cut),
                          source=m)
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
    return Answer(m, f"[{m.label} {detail}{tried}]", ok=False, attempts=attempt,
                  source=m, detail=f"{detail}{tried}")


async def ask_member(m: Member, prompt: str, system: str | None = None,
                     docs: list[Loaded] | None = None) -> Answer:
    """Send one prompt to one seat, down the first of its connections that answers.

    A seat with no backups is a chain of one and behaves exactly as it always
    has. With backups, each is tried in turn — after the one before it has spent
    its own retries — and the first real answer ends the search. Which
    connection produced it travels back on the Answer, because a backup may be
    running a different model than the primary, and a council is a comparison of
    models: a seat that quietly answers as something else is worse than a seat
    that fails, since nothing in the text would give it away.

    Falling through is deliberately not restricted to connection failures. A key
    that was revoked, a model id the relay stopped carrying, a gateway answering
    200 with something that is not an answer — from the seat's point of view
    these are one event: this way in is not currently a way to that model, and
    the config named another. The reasons are not thrown away; every one of them
    comes back with the answer, or in place of it.
    """
    docs = docs or []
    carries_image = any(d.is_image for d in docs)

    trail: list[str] = []          # what each connection, in order, had to say
    last: Answer | None = None
    spent = 0
    for s in m.sources:
        if s.missing:
            trail.append(f"{_where(s)}: not configured — missing {', '.join(s.missing)}")
            continue
        # A blind connection is skipped rather than shown the text alone: the
        # seat has another way to the image, or it has none, and either is
        # better than an answer written about material it never received.
        if carries_image and not s.vision:
            trail.append(f"{_where(s)}: set `vision: false`, and this call carries an image")
            continue
        last = await _ask_one(s, prompt, system, docs)
        spent += last.attempts
        if last.ok:
            return replace(last, member=m, source=s, passed_over=list(trail))
        trail.append(f"{_where(s)}: {last.detail}")

    # Nothing answered. With one connection, the message it already wrote is the
    # whole story; with several, no single one of them is.
    if len(m.sources) == 1:
        if last is not None:
            return replace(last, member=m, source=m)
        if m.missing:
            return Answer(m, f"[{m.label} is not configured — missing "
                             f"{', '.join(m.missing)}. See the server's README for how to "
                             f"configure a council member.]",
                          ok=False, attempts=0, source=m, passed_over=trail)
        # Complete, and still never called: the one thing left that skips a
        # connection is an image it is configured not to be shown.
        return Answer(m, f"[{m.label} was not asked — {trail[0]}]", ok=False, attempts=0,
                      source=m, passed_over=trail)

    listed = "\n".join(f"  {n}. {t}" for n, t in enumerate(trail, start=1))
    return Answer(m, f"[{m.label} has {len(m.sources)} connections configured and none of "
                     f"them answered:\n{listed}]",
                  ok=False, attempts=spent, passed_over=trail)


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
