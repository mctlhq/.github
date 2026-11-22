"""Shared test world. Synthetic data only, no network."""

from __future__ import annotations

import base64
import copy
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPO = ROOT.parents[2]
sys.path.insert(0, str(ROOT))

from model.callback_guard import CallbackGuard  # noqa: E402
from model.clock import FakeClock  # noqa: E402
from model.errors import RpcError  # noqa: E402
from model.recipient import MockRecipient, RecipientTransport  # noqa: E402
from model.server import EventsServer, Policy  # noqa: E402
from model.subscriptions import Auth, Config  # noqa: E402

BINDINGS = {"mctlhq/.github": "tenant-a", "acme/widgets": "tenant-b"}
A = Auth(principal="user-a", tenant="tenant-a")
B = Auth(principal="user-b", tenant="tenant-b")
PR = "github.pull_request.synchronize"
READY = "synthetic.devloop.proposal_ready"


def secret(n: int = 32, fill: int = 7) -> str:
    return "whsec_" + base64.b64encode(bytes([fill]) * n).decode()


def fixture(name: str) -> dict:
    return json.loads((ROOT / "fixtures" / name).read_text())


def pr_event(n: int, repo: str = "mctlhq/.github") -> dict:
    e = fixture("github-pull-request-opened.json")
    e["id"] = f"github:evt-{n}"
    e["correlation_id"] = f"github:evt-{n}"
    e["subject"]["repository"], e["subject"]["number"] = repo, str(n)
    return e


def tenant_of(subject: dict):
    return BINDINGS.get(subject.get("repository"))


def strict_authorize(auth, name, args) -> bool:
    return tenant_of(args) == auth.tenant


class World:
    def __init__(self, policy: Policy | None = None, **cfg) -> None:
        self.clock = FakeClock()
        self.dns = {}
        self.transport = RecipientTransport()
        self.guard = CallbackGuard(lambda host: self.dns.get(host, ["93.184.216.34"]))
        self.server = EventsServer(self.clock, self.guard, self.transport,
                                   policy or Policy(strict_authorize, tenant_of), Config(**cfg))
        self.recipients: dict[str, MockRecipient] = {}

    def recipient(self, url: str, *secrets: str) -> MockRecipient:
        r = MockRecipient(self.clock)
        for s in secrets:
            r.trust(s)
        self.transport.routes[url] = r
        self.recipients[url] = r
        return r

    def rpc(self, method, params, auth=A):
        return self.server.handle(method, params, auth)

    def subscribe(self, url, sec, name=PR, args=None, auth=A, **extra):
        args = {"repository": "mctlhq/.github"} if args is None else args
        params = {"name": name, "arguments": args, "delivery": {"mode": "webhook", "url": url, "secret": sec}}
        params.update(extra)
        return self.rpc("events/subscribe", params, auth)

    def publish(self, env: dict) -> int:
        return self.server.stream.xadd(copy.deepcopy(env))

    def pump(self) -> None:
        self.server.fanout.run_once()
        self.server.worker.run_until_idle()

    def rows(self, sub_id: str, kind: str | None = None):
        return [d for d in self.server.store.deliveries_for(sub_id) if kind is None or d["kind"] == kind]
