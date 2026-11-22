"""Standard Webhooks style signing with client-supplied `whsec_` secrets.

Signed content is `<webhook-id>.<webhook-timestamp>.<body>`, HMAC-SHA256 with the
base64-decoded secret body, header value `v1,<b64>` (space separated when dual
signing). Header names follow ext-events@17c0c64a.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import secrets

from .errors import INVALID_PARAMS, RpcError

PREFIX = "whsec_"


def validate_secret(value) -> bytes:
    """Return the key bytes or raise InvalidParams. Never echoes the value."""
    err = RpcError(INVALID_PARAMS, "delivery.secret must be whsec_ plus base64 of 24 to 64 bytes")
    if not isinstance(value, str) or not value.startswith(PREFIX):
        raise err
    try:
        raw = base64.b64decode(value[len(PREFIX):], validate=True)
    except (binascii.Error, ValueError):
        raise err from None
    if not 24 <= len(raw) <= 64:
        raise err
    return raw


def fingerprint(secret: str) -> str:
    """Non-reversible identifier, the only secret-derived value that is recorded."""
    return hashlib.sha256(secret.encode()).hexdigest()[:12]


def mint_webhook_id() -> str:
    """High-entropy per-message id. Never derived from eventId."""
    return "msg_" + secrets.token_hex(16)


def sign(secret: str, webhook_id: str, timestamp: int, body: bytes) -> str:
    key = validate_secret(secret)
    mac = hmac.new(key, f"{webhook_id}.{timestamp}.".encode() + body, hashlib.sha256).digest()
    return "v1," + base64.b64encode(mac).decode()


def headers_for(secrets_in_force: list[str], webhook_id: str, subscription_id: str,
                timestamp: int, body: bytes) -> dict:
    sigs = " ".join(sign(s, webhook_id, timestamp, body) for s in secrets_in_force)
    return {
        "content-type": "application/json",
        "webhook-id": webhook_id,
        "webhook-timestamp": str(timestamp),
        "webhook-signature": sigs,
        "X-MCP-Subscription-Id": subscription_id,
    }


def verify(secret: str, headers: dict, body: bytes) -> bool:
    try:
        ts = int(headers["webhook-timestamp"])
        wid = headers["webhook-id"]
        got = headers["webhook-signature"].split(" ")
    except (KeyError, ValueError):
        return False
    want = sign(secret, wid, ts, body)
    return any(hmac.compare_digest(want, g) for g in got)
