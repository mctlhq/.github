"""Per-subscription delivery workers.

Each attempt is claimed with a compare-and-set on (subscription_id, epoch,
state=active) before anything is sent, so revocation is linearized at the commit
that sets state=revoked and increments epoch. unsubscribe returns only after
every attempt claimed before the revoke commit has finished or timed out.

Three distinct facts are kept apart: a transport ACK (recipient 2xx, state
`delivered`), persisted per-subscription progress (the delivery row state) and a
processing ACK (`processed`, set only by an explicit tool call, never by a 2xx).
"""

from __future__ import annotations

import hmac
import json
import secrets as _secrets
import threading
from dataclasses import dataclass, field

from . import webhooks
from .callback_guard import GuardError
from .errors import CALLBACK_ENDPOINT_ERROR, NOT_FOUND, RpcError
from .subscriptions import secrets_in_force


@dataclass
class Response:
    status: int
    headers: dict = field(default_factory=dict)
    body: bytes = b""
    elapsed: float = 0.0


class DeliveryWorker:
    def __init__(self, store, subs, guard, transport, clock, config, hooks=None) -> None:
        self.store, self.subs, self.guard = store, subs, guard
        self.transport, self.clock, self.config = transport, clock, config
        self.hooks = hooks or {}
        self.cond = threading.Condition()
        self.inflight: dict[str, int] = {}

    def _hook(self, name: str, **kw) -> None:
        fn = self.hooks.get(name)
        if fn:
            fn(**kw)

    # ---- helpers -------------------------------------------------------
    def _row(self, sub_id: str, webhook_id: str) -> dict | None:
        for d in self.store.deliveries:
            if d["subscription_id"] == sub_id and d["webhook_id"] == webhook_id:
                return d
        return None

    def _post(self, sub: dict, webhook_id: str, body: bytes) -> Response:
        """Re-resolve and pin the address, sign fresh, POST. Redirects are not followed."""
        _host, ip, _port = self.guard.check(sub["url"])
        now = self.clock.now()
        headers = webhooks.headers_for(secrets_in_force(sub, now), webhook_id, sub["id"], int(now), body)
        return self.transport.post(sub["url"], ip, headers, body, self.config.http_timeout_s)

    @staticmethod
    def _body(payload: dict) -> bytes:
        return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()

    def _record_control(self, sub: dict, kind: str, wid: str, state: str) -> None:
        with self.store.transaction():
            self.store.deliveries.append({
                "subscription_id": sub["id"], "event_id": f"{kind}:{wid}", "kind": kind, "payload": {"type": kind},
                "webhook_id": wid, "sub_seq": sub["next_seq"], "state": state, "attempts": 1,
                "next_attempt_at": 0.0, "first_attempt_at": self.clock.now(), "claimed_at": None,
                "processed": False, "correlation_id": None,
            })
            self.store.subscriptions[sub["id"]]["next_seq"] += 1
            self.store.record_audit(kind, subscription_id=sub["id"], webhook_id=wid)

    # ---- verification and termination ---------------------------------
    def verify_callback(self, sub: dict) -> None:
        wid = webhooks.mint_webhook_id()
        challenge = _secrets.token_urlsafe(24)
        body = self._body({"type": "verification", "challenge": challenge})
        ok = False
        try:
            resp = self._post(sub, wid, body)
            if 200 <= resp.status < 300 and resp.elapsed <= self.config.http_timeout_s:
                echoed = json.loads(resp.body or b"{}").get("challenge", "")
                ok = isinstance(echoed, str) and hmac.compare_digest(echoed.encode(), challenge.encode())
        except (GuardError, ValueError, AttributeError):
            ok = False
        self._record_control(sub, "verification", wid, "delivered" if ok else "failed_final")
        if not ok:
            raise RpcError(CALLBACK_ENDPOINT_ERROR, "callback endpoint verification failed")
        with self.store.transaction():
            self.store.subscriptions[sub["id"]]["state"] = "active"

    def terminate(self, sub_id: str, reason: str) -> None:
        """Server-initiated termination: revoke, then a signed `terminated` envelope."""
        if not self.subs.revoke(sub_id, reason):
            return
        sub = self.store.subscriptions[sub_id]
        wid = webhooks.mint_webhook_id()
        try:
            resp = self._post(sub, wid, self._body({"type": "terminated", "reason": reason}))
            state = "delivered" if 200 <= resp.status < 300 else "failed_final"
        except GuardError:
            state = "failed_final"
        self._record_control(sub, "terminated", wid, state)

    def expire_due(self) -> None:
        now = self.clock.now()
        for s in list(self.store.subscriptions.values()):
            if s["state"] == "active" and now >= s["expires_at"]:
                self.terminate(s["id"], "expired")

    # ---- unsubscribe drain --------------------------------------------
    def drain(self, sub_id: str) -> bool:
        """Wait (bounded by the HTTP timeout) for attempts claimed before the revoke."""
        with self.cond:
            return self.cond.wait_for(lambda: self.inflight.get(sub_id, 0) == 0,
                                      timeout=self.config.http_timeout_s)

    # ---- attempts -------------------------------------------------------
    def attempt(self, sub_id: str, webhook_id: str, epoch: int) -> str:
        self._hook("before_claim", sub_id=sub_id, webhook_id=webhook_id)
        with self.store.transaction():  # compare-and-set claim
            sub = self.store.subscriptions[sub_id]
            d = self._row(sub_id, webhook_id)
            if sub["state"] != "active" or sub["epoch"] != epoch:
                return "not_claimed"
            if d is None or d["state"] not in ("pending", "retry"):
                return "not_claimed"
            now = self.clock.now()
            d["state"], d["claimed_at"] = "inflight", now
            d["attempts"] += 1
            d["first_attempt_at"] = d["first_attempt_at"] if d["first_attempt_at"] is not None else now
            with self.cond:
                self.inflight[sub_id] = self.inflight.get(sub_id, 0) + 1
        try:
            self._hook("after_claim", sub_id=sub_id, webhook_id=webhook_id)
            sub = self.store.subscriptions[sub_id]
            reason, status = None, None
            try:
                resp = self._post(sub, webhook_id, self._body(self._row(sub_id, webhook_id)["payload"]))
                status = resp.status
                if resp.elapsed > self.config.http_timeout_s:
                    status, reason = None, "timeout"
            except GuardError:
                reason = "guard"
            self._hook("after_post", sub_id=sub_id, webhook_id=webhook_id)
            self._finish(sub_id, webhook_id, status, reason)
            return "sent"
        finally:
            with self.cond:
                self.inflight[sub_id] -= 1
                self.cond.notify_all()

    def _finish(self, sub_id: str, webhook_id: str, status, reason) -> None:
        now = self.clock.now()
        revoke_after = False
        with self.store.transaction():
            d = self._row(sub_id, webhook_id)
            audit = dict(envelope_id=d["event_id"] if d["kind"] == "event" else None,
                         correlation_id=d["correlation_id"], subscription_id=sub_id, webhook_id=webhook_id)
            if status is not None and 200 <= status < 300:
                d["state"] = "delivered"  # transport ACK only; `processed` stays False
                self.store.record_audit("delivered", **audit)
            elif status == 410:
                d["state"] = "failed_final"
                revoke_after = True
                self.store.record_audit("recipient_gone", **audit)
            elif status == 413:
                d["state"] = "failed_final"
                self.store.record_audit("payload_too_large", **audit)
            else:
                exhausted = (d["attempts"] >= self.config.max_attempts
                             or now - d["first_attempt_at"] >= self.config.max_elapsed_s)
                why = reason or f"http_{status}"  # 3xx lands here: redirects are never followed
                if exhausted:
                    d["state"] = "dead_letter"
                    self.store.record_audit("dead_letter", reason=why, **audit)
                else:
                    delay = min(self.config.cap_backoff_s, self.config.base_backoff_s * 2 ** (d["attempts"] - 1))
                    d["state"], d["next_attempt_at"] = "retry", now + delay
                    self.store.record_audit("retry", reason=why, **audit)
        if revoke_after:
            self.subs.revoke(sub_id, "recipient_gone")

    # ---- scheduling ----------------------------------------------------
    def recover_inflight(self) -> None:
        """Rows stuck inflight past the HTTP timeout (crash) are retried with the same webhook-id."""
        now = self.clock.now()
        with self.store.transaction():
            for d in self.store.deliveries:
                if d["state"] == "inflight" and now >= d["claimed_at"] + self.config.http_timeout_s:
                    d["state"], d["next_attempt_at"] = "retry", now
                    self.store.record_audit("inflight_recovered", subscription_id=d["subscription_id"],
                                            webhook_id=d["webhook_id"])

    def tick(self) -> int:
        self.recover_inflight()
        self.expire_due()
        sent = 0
        for sub in list(self.store.subscriptions.values()):
            if sub["state"] != "active":
                continue
            epoch = sub["epoch"]
            for d in self.store.deliveries_for(sub["id"]):
                if d["state"] in ("pending", "retry") and d["next_attempt_at"] <= self.clock.now():
                    if self.attempt(sub["id"], d["webhook_id"], epoch) == "sent":
                        sent += 1
        return sent

    def next_due(self) -> float | None:
        times = [d["next_attempt_at"] for d in self.store.deliveries
                 if d["state"] in ("pending", "retry")
                 and self.store.subscriptions[d["subscription_id"]]["state"] == "active"]
        return min(times) if times else None

    def run_until_idle(self, limit: int = 200) -> None:
        for _ in range(limit):
            self.tick()
            nxt = self.next_due()
            if nxt is None:
                return
            if nxt > self.clock.now():
                self.clock.t = nxt

    # ---- processing ACK ------------------------------------------------
    def processing_ack(self, principal: str, sub_id: str, event_id: str) -> None:
        with self.store.transaction():
            sub = self.store.subscriptions.get(sub_id)
            if sub is None or sub["principal"] != principal:
                raise RpcError(NOT_FOUND, "unknown subscription")
            for d in self.store.deliveries_for(sub_id):
                if d["event_id"] == event_id and d["kind"] == "event" and d["state"] == "delivered":
                    d["processed"] = True
                    self.store.record_audit("processed", envelope_id=event_id, subscription_id=sub_id,
                                            webhook_id=d["webhook_id"], correlation_id=d["correlation_id"])
                    return
            raise RpcError(NOT_FOUND, "no delivered event to acknowledge")
