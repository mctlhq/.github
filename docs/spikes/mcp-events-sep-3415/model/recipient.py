"""Mock webhook recipient (in-process, read-only).

Verifies signatures against every known secret per subscription, rejects stale
timestamps (older than 5 minutes), deduplicates on webhook-id per endpoint and on
eventId only within one subscription. A replayed webhook-id returns HTTP 200 with
`duplicate: true` and no second side effect (so the sender stops retrying); it is
counted as rejected. That 200-on-duplicate choice is [INFERRED], not a spec rule.
"""

from __future__ import annotations

import json

from . import webhooks
from .delivery import Response

TOLERANCE_S = 300


class MockRecipient:
    def __init__(self, clock) -> None:
        self.clock = clock
        self.secrets: dict[str, list[str]] = {}  # subscription id -> secrets
        self.default_secrets: list[str] = []
        self.seen_webhook_ids: set[str] = set()
        self.seen_events: set[tuple[str, str]] = set()
        self.accepted: list[dict] = []  # side effects
        self.rejected: list[str] = []
        self.control: list[dict] = []
        self.force_status: int | None = None
        self.redirect = False

    def trust(self, secret: str, subscription_id: str | None = None) -> None:
        if subscription_id is None:
            self.default_secrets.append(secret)
        else:
            self.secrets.setdefault(subscription_id, []).append(secret)

    def receive(self, headers: dict, body: bytes) -> Response:
        if self.redirect:
            return Response(302, {"location": "https://169.254.169.254/latest"})
        if self.force_status is not None:
            return Response(self.force_status)
        try:
            ts, wid = int(headers["webhook-timestamp"]), headers["webhook-id"]
            sub_id = headers["X-MCP-Subscription-Id"]
        except (KeyError, ValueError):
            self.rejected.append("missing_header")
            return Response(400)
        if abs(self.clock.now() - ts) > TOLERANCE_S:
            self.rejected.append("stale_timestamp")
            return Response(400)
        keys = self.secrets.get(sub_id, []) + self.default_secrets
        if not any(webhooks.verify(k, headers, body) for k in keys):
            self.rejected.append("bad_signature")
            return Response(401)
        if wid in self.seen_webhook_ids:
            self.rejected.append("duplicate_webhook_id")
            return Response(200, body=b'{"duplicate":true}')
        self.seen_webhook_ids.add(wid)
        msg = json.loads(body)
        if msg.get("type") == "verification":
            self.control.append(msg)
            return Response(200, body=json.dumps({"challenge": msg["challenge"]}).encode())
        if msg.get("type") in ("gap", "terminated"):
            self.control.append(msg)
            return Response(200)
        key = (sub_id, msg["eventId"])
        if key in self.seen_events:
            self.rejected.append("duplicate_event_in_subscription")
            return Response(200, body=b'{"duplicate":true}')
        self.seen_events.add(key)
        self.accepted.append({"webhook_id": wid, "subscription_id": sub_id, "eventId": msg["eventId"], "msg": msg})
        return Response(200)


class RecipientTransport:
    """Routes POSTs to MockRecipients by URL and records every request (captures)."""

    def __init__(self) -> None:
        self.routes: dict[str, MockRecipient] = {}
        self.requests: list[dict] = []

    def post(self, url: str, ip: str, headers: dict, body: bytes, timeout: float) -> Response:
        self.requests.append({"url": url, "ip": ip, "headers": dict(headers), "body": body})
        return self.routes[url].receive(headers, body)
