"""Reference client with capability negotiation and the existing-fallback plan."""

from __future__ import annotations

EXTENSION_ID = "io.modelcontextprotocol/events"

# Fallbacks that exist today. They share no Events cursor, ordering or dedupe.
FALLBACKS = (
    {"kind": "claude-channel", "scope": "an active Claude Code session (mctl-claude-remote)", "shares_events_cursor": False},
    {"kind": "host-scheduled-reads", "scope": "existing authorized read tools such as "
     "mctl_get_dev_loop, mctl_list_incidents, gh pr view", "shares_events_cursor": False},
)


class ExtensionNotAdvertised(Exception):
    pass


class ReferenceClient:
    def __init__(self, server, auth) -> None:
        self.server, self.auth = server, auth
        self.caps = server.capabilities()
        self.sent: list[str] = []

    def supports_events(self) -> bool:
        return EXTENSION_ID in self.caps.get("extensions", {})

    def call(self, method: str, params: dict | None = None):
        if method.startswith("events/") and not self.supports_events():
            raise ExtensionNotAdvertised(method)  # nothing is sent
        self.sent.append(method)
        return self.server.handle(method, params, self.auth)

    def plan(self) -> dict:
        if self.supports_events():
            return {"mode": "events", "fallbacks": []}
        return {"mode": "fallback", "fallbacks": [dict(f) for f in FALLBACKS]}
