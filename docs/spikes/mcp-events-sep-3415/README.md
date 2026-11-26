# MCP Events (SEP-3415) reference model

Offline, stdlib-only model for mctlhq/.github#377. It proves the delivery
contract of a thin `io.modelcontextprotocol/events` adapter over the existing
MCTL event path, and nothing else. No network, no live host, no real content.
The report is `docs/spikes/mcp-events-sep-3415.md`; the spec pin is
`model/SPEC_PIN.md`.

Run (expect `OK`, 37 tests):

    python3 -I -m unittest discover docs/spikes/mcp-events-sep-3415/tests

Layout:

| Path | Role |
|---|---|
| `model/stream.py` | fake Valkey stream: XADD with MAXLEN, XREADGROUP, XACK, XAUTOCLAIM |
| `model/store.py` | transactional store standing in for mctl-api's database, with crash points |
| `model/subscriptions.py` | key, secret refresh, TTL, epoch, revocation commit |
| `model/projection.py` | envelope v1 to `EventOccurrence` (references only) |
| `model/fanout.py` | materialize in one transaction, then XACK |
| `model/delivery.py` | per-subscription workers, CAS claim, retry, drain on unsubscribe |
| `model/cursor.py` | `null`, `maxAgeMs`, `truncated`, `hasMore` |
| `model/webhooks.py` | client-supplied secrets, signing, dual-sign, fingerprints |
| `model/callback_guard.py` | SSRF guard with an injectable resolver |
| `model/server.py`, `client.py` | capability negotiation, fallback plan |
| `model/recipient.py` | in-process mock recipient |
| `fixtures/` | synthetic envelopes only |
| `tests/` | T1 to T22 (see the report, section 3 and the task list) |

Model limits: the push stream notifications are not simulated, the verification
echo format is a model choice, and nothing here has touched a real host.
Test ids T1 to T22 appear in the test method names.
