"""SSRF guard for webhook callback URLs.

HTTPS only, no userinfo, no IP literals, port allowlist, every resolved address
must be globally routable. The resolver is injectable and the guard is run at
registration and again at every delivery; the connection goes to the pinned IP.
Redirects are never followed (the transport contract returns 3xx as a failure).
"""

from __future__ import annotations

import ipaddress
from urllib.parse import urlsplit


class GuardError(Exception):
    pass


def _globally_routable(addr: str) -> bool:
    ip = ipaddress.ip_address(addr)
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_global and not (ip.is_multicast or ip.is_unspecified or ip.is_loopback
                                 or ip.is_link_local or ip.is_private or ip.is_reserved)


class CallbackGuard:
    def __init__(self, resolver, allowed_ports=(443,)) -> None:
        self.resolver = resolver  # host -> list[str]
        self.allowed_ports = set(allowed_ports)

    def check(self, url: str) -> tuple[str, str, int]:
        """Return (host, pinned_ip, port) or raise GuardError."""
        try:
            parts = urlsplit(url)
            port = parts.port or 443
        except ValueError:
            raise GuardError("unparseable url") from None
        if parts.scheme != "https":
            raise GuardError("https required")
        if parts.username is not None or parts.password is not None or "@" in parts.netloc:
            raise GuardError("userinfo not allowed")
        host = parts.hostname
        if not host:
            raise GuardError("host required")
        try:
            ipaddress.ip_address(host)
        except ValueError:
            pass
        else:
            raise GuardError("ip literal not allowed")
        if port not in self.allowed_ports:
            raise GuardError("port not allowed")
        addrs = self.resolver(host)
        if not addrs:
            raise GuardError("no address")
        for a in addrs:
            if not _globally_routable(a):
                raise GuardError("non-global address")
        return host, addrs[0], port
