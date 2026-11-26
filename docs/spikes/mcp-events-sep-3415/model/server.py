"""Reference MCP server surface for io.modelcontextprotocol/events (pinned spec).

Composition root for the model. Without the advertised extension every `events/*`
request is answered with -32601. `events/stream` models activation only (cursor
and truncated); the push notifications are not simulated.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from . import cursor as cursor_mod
from .callback_guard import GuardError
from .delivery import DeliveryWorker
from .errors import (CALLBACK_ENDPOINT_ERROR, FORBIDDEN, INVALID_PARAMS, METHOD_NOT_FOUND, NOT_FOUND, RpcError)
from .fanout import Fanout, backfill
from .projection import PAYLOAD_SCHEMA
from .store import Store
from .stream import Stream
from .subscriptions import Auth, Config, SubscriptionService, matches, sub_key

EXTENSION_ID = "io.modelcontextprotocol/events"

DEFAULT_CATALOG = {
    "synthetic.devloop.proposal_ready": "A synthetic DevLoop proposal is ready for review.",
    "github.pull_request.synchronize": "A pull request head moved.",
    "synthetic.ping.created": "Synthetic ping.",
}


@dataclass
class Policy:
    authorize: callable  # (Auth, name, arguments) -> bool
    tenant_of: callable  # subject -> tenant or None


class EventsServer:
    def __init__(self, clock, guard, transport, policy: Policy, config: Config | None = None,
                 advertise: bool = True, catalog: dict | None = None, hooks: dict | None = None,
                 stream: Stream | None = None) -> None:
        self.clock, self.policy = clock, policy
        self.config = config or Config()
        self.advertise = advertise
        self.catalog = catalog or DEFAULT_CATALOG
        self.store = Store()
        self.stream = stream or Stream("mctl:events:synthetic", clock)
        self.subs = SubscriptionService(self.store, clock, self.config, self.catalog, policy.authorize)
        self.fanout = Fanout(self.stream, self.store, self.config, policy.tenant_of)
        self.worker = DeliveryWorker(self.store, self.subs, guard, transport, clock, self.config, hooks)
        self.guard = guard
        self.log: list[str] = []

    def capabilities(self) -> dict:
        return {"extensions": {EXTENSION_ID: {}} if self.advertise else {}}

    @staticmethod
    def _iso(ts: float) -> str:
        return datetime.fromtimestamp(ts, timezone.utc).isoformat()

    def handle(self, method: str, params: dict | None, auth: Auth | None):
        self.log.append(f"{method} principal={auth.principal if auth else None}")  # never params
        try:
            return self._dispatch(method, params or {}, auth)
        except RpcError as e:
            self.log.append(f"error {e.code}")
            raise

    def _dispatch(self, method: str, params: dict, auth: Auth | None):
        if method == "mctl/ack_event":  # MCTL tool, not an events/* method
            if auth is None:
                raise RpcError(FORBIDDEN, "authentication required")
            self.worker.processing_ack(auth.principal, params.get("subscriptionId", ""), params.get("eventId", ""))
            return {}
        if not method.startswith("events/") or not self.advertise:
            raise RpcError(METHOD_NOT_FOUND, "method not found")
        if auth is None:
            raise RpcError(FORBIDDEN, "authentication required")
        if method == "events/list":
            return {"events": [{"name": n, "description": d, "payloadSchema": PAYLOAD_SCHEMA}
                               for n, d in self.catalog.items()]}
        if method in ("events/poll", "events/stream"):
            return self._read(method, params, auth)
        if method == "events/subscribe":
            return self._subscribe(params, auth)
        if method == "events/unsubscribe":
            return self._unsubscribe(params, auth)
        raise RpcError(METHOD_NOT_FOUND, "method not found")

    def _check_name(self, params: dict, auth: Auth):
        name, args = params.get("name"), params.get("arguments", {})
        if not isinstance(name, str) or not isinstance(args, dict):
            raise RpcError(INVALID_PARAMS, "name and arguments required")
        if name not in self.catalog:
            raise RpcError(NOT_FOUND, "unknown event")
        if not self.policy.authorize(auth, name, args):
            raise RpcError(FORBIDDEN, "not authorized")
        return name, args

    def _read(self, method: str, params: dict, auth: Auth):
        name, args = self._check_name(params, auth)
        c, age = params.get("cursor"), params.get("maxAgeMs")
        if method == "events/stream":
            start = cursor_mod.resolve_start(self.stream, c, age, self.clock.now_ms())
            return {"active": {"cursor": cursor_mod.encode(start["after"]), "truncated": start["truncated"]}}
        max_events = min(max(int(params.get("maxEvents", 100)), 1), 1000)

        def accept(env) -> bool:
            try:
                return (env["type"] == name and matches({"name": name, "arguments": args}, env)
                        and self.policy.tenant_of(env["subject"]) == auth.tenant)
            except (KeyError, TypeError):
                return False

        return cursor_mod.poll(self.stream, c, age, max_events, self.clock.now_ms(),
                               audit=lambda stage: self.store.record_audit(stage), accept=accept)

    def _subscribe(self, params: dict, auth: Auth):
        c = params.get("cursor")
        start = cursor_mod.resolve_start(self.stream, c, params.get("maxAgeMs") if c is not None else None,
                                         self.clock.now_ms())
        # registration-time guard: a bad callback fails before any state is kept
        url = (params.get("delivery") or {}).get("url") if isinstance(params.get("delivery"), dict) else None
        sub, created = self.subs.upsert(auth, params, start["after"])
        if created:
            try:
                self.guard.check(url)
                self.worker.verify_callback(sub)
            except GuardError:
                self._drop(sub["id"])
                raise RpcError(CALLBACK_ENDPOINT_ERROR, "callback endpoint rejected") from None
            except RpcError:
                self._drop(sub["id"])
                raise
        if c is not None:
            backfill(self.fanout, sub["id"], start["after"], start["truncated"])
        out_cursor = cursor_mod.encode(self.stream.tail() if c is None else start["after"])
        return {"id": sub["id"], "cursor": out_cursor, "truncated": start["truncated"],
                "refreshBefore": self._iso(sub["expires_at"])}

    def _drop(self, sub_id: str) -> None:
        with self.store.transaction():
            self.store.subscriptions.pop(sub_id, None)

    def _unsubscribe(self, params: dict, auth: Auth):
        d = params.get("delivery") or {}
        key = sub_key(auth.principal, d.get("url"), params.get("name"), params.get("arguments", {}))
        sub = self.subs.find(key)
        if sub is None:
            raise RpcError(NOT_FOUND, "no such subscription")
        self.subs.revoke(sub["id"], "unsubscribed")  # linearization point
        self.worker.drain(sub["id"])  # nothing in flight once this returns
        return {}
