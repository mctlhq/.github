"""Subscription identity, secret handling, TTL and epoch.

Key: (principal, delivery.url, name, arguments), per events.mdx. Tenant,
principal and client come from authentication, never from arguments.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from .errors import FORBIDDEN, INVALID_PARAMS, NOT_FOUND, RESOURCE_EXHAUSTED, UNSUPPORTED, RpcError
from . import webhooks


@dataclass(frozen=True)
class Auth:
    principal: str
    tenant: str
    client: str = "oauth-client"


@dataclass
class Config:
    default_ttl_s: int = 3600
    max_ttl_s: int = 86400
    secret_grace_s: int = 300
    max_subscriptions_per_principal: int = 5
    per_subscription_quota: int = 100  # undelivered rows before materialization pauses
    max_attempts: int = 5
    base_backoff_s: float = 1.0
    cap_backoff_s: float = 60.0
    max_elapsed_s: float = 3600.0
    http_timeout_s: float = 5.0


def sub_key(principal: str, url: str, name: str, arguments: dict) -> str:
    return json.dumps([principal, url, name, arguments], sort_keys=True, separators=(",", ":"))


def matches(sub: dict, env: dict) -> bool:
    if env["type"] != sub["name"]:
        return False
    return all(env["subject"].get(k) == v for k, v in sub["arguments"].items())


def secrets_in_force(sub: dict, now: float) -> list[str]:
    out = [sub["secret_current"]]
    if sub["secret_previous"] and now < sub["grace_until"]:
        out.append(sub["secret_previous"])  # dual-sign during the grace window
    return out


class SubscriptionService:
    def __init__(self, store, clock, config: Config, catalog: dict, authorize) -> None:
        self.store, self.clock, self.config = store, clock, config
        self.catalog = catalog  # event name -> description
        self.authorize = authorize  # (auth, name, arguments) -> bool

    def find(self, key: str) -> dict | None:
        for s in self.store.subscriptions.values():
            if s["key"] == key and s["state"] != "revoked":
                return s
        return None

    def grant_ttl(self, suggested) -> int:
        ttl = self.config.default_ttl_s if not isinstance(suggested, (int, float)) or isinstance(suggested, bool) \
            else int(suggested / 1000)
        return max(1, min(ttl, self.config.max_ttl_s))

    def upsert(self, auth: Auth | None, params: dict, start_after: int) -> tuple[dict, bool]:
        """Create or refresh. Returns (subscription, created). Caller verifies new ones."""
        if auth is None:
            raise RpcError(FORBIDDEN, "authentication required")
        if not isinstance(params, dict):
            raise RpcError(INVALID_PARAMS, "params must be an object")
        delivery = params.get("delivery")
        name, arguments = params.get("name"), params.get("arguments", {})
        if not isinstance(delivery, dict) or not isinstance(name, str) or not isinstance(arguments, dict):
            raise RpcError(INVALID_PARAMS, "name, arguments and delivery are required")
        if delivery.get("mode") != "webhook":
            raise RpcError(UNSUPPORTED, "only webhook delivery is modelled for subscribe")
        url = delivery.get("url")
        if not isinstance(url, str):
            raise RpcError(INVALID_PARAMS, "delivery.url required")
        secret = delivery.get("secret")
        webhooks.validate_secret(secret)  # InvalidParams; the model never generates one
        if name not in self.catalog:
            raise RpcError(NOT_FOUND, "unknown event")
        if not self.authorize(auth, name, arguments):
            raise RpcError(FORBIDDEN, "not authorized for this event or arguments")
        now = self.clock.now()
        key = sub_key(auth.principal, url, name, arguments)
        ttl = self.grant_ttl(params.get("ttlMs"))
        with self.store.transaction():
            sub = self.find(key)
            if sub is not None:
                if secret != sub["secret_current"]:
                    sub["secret_previous"] = sub["secret_current"]
                    sub["secret_current"] = secret
                    sub["grace_until"] = now + self.config.secret_grace_s
                    self.store.record_audit("secret_refreshed", subscription_id=sub["id"],
                                            fingerprint=webhooks.fingerprint(secret))
                sub["expires_at"] = now + ttl
                return sub, False
            mine = [s for s in self.store.subscriptions.values()
                    if s["principal"] == auth.principal and s["state"] != "revoked"]
            if len(mine) >= self.config.max_subscriptions_per_principal:
                raise RpcError(RESOURCE_EXHAUSTED, "too many subscriptions")
            sid = self.store.next_id("sub")
            sub = {
                "id": sid, "key": key, "tenant": auth.tenant, "principal": auth.principal,
                "client": auth.client, "name": name, "arguments": dict(arguments), "url": url,
                "secret_current": secret, "secret_previous": None, "grace_until": 0.0,
                "state": "pending", "epoch": 1, "expires_at": now + ttl, "start_after": start_after,
                "gap_from": None, "next_seq": 1,
            }
            self.store.subscriptions[sid] = sub
            self.store.record_audit("subscribed", subscription_id=sid,
                                    fingerprint=webhooks.fingerprint(secret))
            return sub, True

    def revoke(self, sub_id: str, reason: str) -> bool:
        """Linearization point: commit of state=revoked and epoch+1."""
        with self.store.transaction():
            sub = self.store.subscriptions[sub_id]
            if sub["state"] == "revoked":
                return False
            sub["state"] = "revoked"
            sub["epoch"] += 1
            self.store.record_audit("revoked", subscription_id=sub_id, reason=reason)
            return True
