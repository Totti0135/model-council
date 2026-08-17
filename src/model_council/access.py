"""Who may reach the server once it listens on a network.

Over stdio the operating system settles this question: the client launches the
process, so whoever runs it already had the keys. HTTP removes that guarantee.
One process now holds one set of provider keys and answers whoever can reach the
port — which is the entire point, since nobody else then needs a key, and also
the entire risk, because an open port in front of paid provider quota is an open
bar.

So an HTTP server refuses to start without an allowlist, and this module is what
it checks against: the peer's address, resolved through any reverse proxy that
was explicitly declared trusted, plus a refusal to serve browsers.
"""
from __future__ import annotations

import logging
from ipaddress import IPv4Network, IPv6Network, ip_address, ip_network
from typing import Iterable

from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Receive, Scope, Send

log = logging.getLogger("model_council.access")

Network = IPv4Network | IPv6Network
Address = object  # IPv4Address | IPv6Address, kept loose for the helpers below

# Always reachable, and not worth defending: a caller on the loopback interface
# is on the machine that holds the config file the keys came from, so refusing
# it protects nothing and only makes `curl localhost` during a deploy confusing.
LOOPBACK: tuple[Network, ...] = (ip_network("127.0.0.0/8"), ip_network("::1/128"))

# Names for the ranges nobody wants to retype. `any` is deliberately spellable —
# there are real reasons to put this behind something else that does the
# filtering — but it has to be typed out, so it cannot happen by accident.
ALIASES: dict[str, tuple[str, ...]] = {
    "loopback": ("127.0.0.0/8", "::1/128"),
    "private": ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "fc00::/7"),
    "any": ("0.0.0.0/0", "::/0"),
}


def parse_networks(spec: str | Iterable[str]) -> list[Network]:
    """Turn `10.0.0.0/8, private, 192.168.1.7` into networks.

    Accepts CIDRs, bare addresses (a bare address is a host route, not a typo
    worth guessing at), and the names in ALIASES. Separators are commas or
    whitespace, so a value pasted from a shell, a compose file or a systemd unit
    all parse the same way.
    """
    tokens: list[str] = []
    for chunk in ([spec] if isinstance(spec, str) else list(spec)):
        tokens += [t for t in str(chunk).replace(",", " ").split() if t]

    out: list[Network] = []
    for t in tokens:
        if t.lower() in ALIASES:
            out += [ip_network(a) for a in ALIASES[t.lower()]]
            continue
        try:
            out.append(ip_network(t, strict=False))
        except ValueError:
            raise ValueError(
                f"{t!r} is not a network, an address, or one of "
                f"{', '.join(sorted(ALIASES))}"
            ) from None
    return out


def _addr(raw: str):
    """Parse an address, unwrapping the IPv4-mapped IPv6 form.

    A dual-stack listener reports an IPv4 peer as `::ffff:10.0.0.1`, which
    matches no IPv4 network and would silently fail every allowlist written the
    obvious way.
    """
    a = ip_address(raw.strip())
    return a.ipv4_mapped if getattr(a, "ipv4_mapped", None) else a


def _in(addr, nets: Iterable[Network]) -> bool:
    return any(addr in n for n in nets)


def client_ip(scope: Scope, trusted: Iterable[Network]) -> tuple[object | None, str]:
    """The address to hold against the allowlist, and how it was determined.

    The peer is the truth unless the peer is a reverse proxy we were told to
    trust; only then does `X-Forwarded-For` mean anything. That condition is the
    whole security of this function. Trusting the header unconditionally would
    let any caller write `X-Forwarded-For: 10.0.0.1` and walk through the
    allowlist, and ignoring it entirely would collapse every client behind a
    proxy into the proxy's own address, which is just as useless.

    Within a trusted chain the real client is the rightmost entry that is not
    itself a trusted proxy: entries to the left of it were written by hops we
    have no reason to believe.
    """
    peer = scope.get("client")
    if not peer:
        return None, "no peer address"
    try:
        addr = _addr(peer[0])
    except ValueError:
        return None, f"unparseable peer address {peer[0]!r}"

    trusted = list(trusted)
    if not _in(addr, trusted):
        return addr, "peer"

    raw = b""
    for k, v in scope.get("headers") or []:
        if k == b"x-forwarded-for":
            raw = v
            break
    for hop in reversed(raw.decode("latin-1").split(",")):
        if not hop.strip():
            continue
        try:
            candidate = _addr(hop)
        except ValueError:
            return None, f"unparseable X-Forwarded-For entry {hop.strip()!r}"
        if not _in(candidate, trusted):
            return candidate, "x-forwarded-for"
    # Everything in the chain is a proxy we trust, so the proxy is the client.
    return addr, "peer (trusted proxy, no forwarded client)"


class ClientGate:
    """ASGI middleware: an address allowlist, and no browsers.

    Wraps the whole app rather than one route, health check included — an
    endpoint that answers from anywhere is a way to confirm the service exists
    and where, which is exactly what an allowlist is meant to withhold.

    The browser rule is the less obvious half. An allowlist admits every machine
    on the office network, and every one of those machines runs a browser that
    will issue requests on behalf of whatever page it happens to have open. MCP
    clients are not browsers and send no `Origin`; a page always does. So a
    request carrying `Origin` is refused: without it, any internal employee
    visiting any website could have their browser spend the council's quota.
    """

    def __init__(self, app: ASGIApp, *, allow: Iterable[Network],
                 trusted_proxies: Iterable[Network] = (),
                 allowed_origins: Iterable[str] = ()) -> None:
        self.app = app
        self.allow = list(allow) + [n for n in LOOPBACK]
        self.trusted = list(trusted_proxies)
        self.origins = {o.rstrip("/") for o in allowed_origins}

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        # `lifespan` has no peer to check, and blocking it would stop the app
        # from starting at all.
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        deny = self._deny_reason(scope)
        if deny is None:
            await self.app(scope, receive, send)
            return

        log.warning("refused %s %s — %s", scope.get("method", "?"),
                    scope.get("path", "?"), deny)
        # The caller is told it was refused and nothing else. Which rule it
        # tripped, and what would satisfy that rule, is for the operator's log.
        await PlainTextResponse("Forbidden", status_code=403)(scope, receive, send)

    def _deny_reason(self, scope: Scope) -> str | None:
        origin = ""
        for k, v in scope.get("headers") or []:
            if k == b"origin":
                origin = v.decode("latin-1").strip().rstrip("/")
                break
        if origin and origin not in self.origins:
            return f"browser origin {origin!r} (this endpoint serves MCP clients, not pages)"

        addr, how = client_ip(scope, self.trusted)
        if addr is None:
            return how
        if not _in(addr, self.allow):
            return f"address {addr} (via {how}) is not in the allowlist"
        return None
