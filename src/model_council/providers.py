"""Wire-format transports.

Two shapes cover nearly every endpoint worth talking to: the OpenAI Chat
Completions format (`POST {base_url}/chat/completions`) and the Anthropic
Messages format (`POST {base_url}/v1/messages`). Failures are returned as
readable strings rather than raised, so one unreachable member degrades the
answer instead of killing the whole call.
"""
from __future__ import annotations

import httpx

from model_council.config import Member


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
    async with httpx.AsyncClient(timeout=m.timeout) as client:
        r = await client.post(m.base_url + "/chat/completions", json=payload, headers=headers)
        r.raise_for_status()
        data = r.json()
    try:
        return data["choices"][0]["message"]["content"] or "[empty response]"
    except (KeyError, IndexError, TypeError):
        return f"[unexpected response shape: {str(data)[:600]}]"


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
    async with httpx.AsyncClient(timeout=m.timeout) as client:
        r = await client.post(m.base_url + "/v1/messages", json=payload, headers=headers)
        r.raise_for_status()
        data = r.json()
    parts = [b.get("text", "") for b in data.get("content", []) if b.get("type") == "text"]
    return "".join(parts) or f"[unexpected response shape: {str(data)[:600]}]"


async def ask_member(m: Member, prompt: str, system: str | None = None) -> str:
    """Send one prompt to one member, never raising."""
    if not m.configured:
        return (f"[{m.label} is not configured — missing {', '.join(m.missing)}. "
                f"See the server's README for how to configure a council member.]")
    try:
        if m.format == "anthropic":
            return await _call_anthropic(m, prompt, system)
        return await _call_openai(m, prompt, system)
    except httpx.HTTPStatusError as e:
        return f"[{m.label} HTTP {e.response.status_code}: {e.response.text[:500]}]"
    except httpx.RequestError as e:
        return f"[{m.label} network error: {type(e).__name__}: {e}]"
    except Exception as e:  # noqa: BLE001 - surface any failure back to the caller
        return f"[{m.label} error: {type(e).__name__}: {e}]"


async def probe_member(m: Member) -> str:
    """Ask one member's endpoint which model ids it exposes."""
    if m.format == "anthropic":
        url = m.base_url + "/v1/models"
        headers = {"x-api-key": m.api_key, "anthropic-version": "2023-06-01", **m.headers}
    else:
        url = m.base_url + "/models"
        headers = {"Authorization": f"Bearer {m.api_key}", **m.headers}
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(url, headers=headers)
        r.raise_for_status()
        data = r.json()
    ids = [x.get("id", "?") for x in data.get("data", [])]
    return ", ".join(ids[:60]) if ids else str(data)[:400]
