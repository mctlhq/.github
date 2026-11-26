"""Envelope v1 to MCP `EventOccurrence`.

eventId = envelope id (stable, the dedupe key for the occurrence). data carries
envelope references only. The envelope is validated against the closed v1 shape
here (stdlib); events/tests/test_envelope_schema.py remains the schema authority.
"""

from __future__ import annotations

import re

REQUIRED = ("specversion", "id", "type", "source", "occurred_at", "correlation_id", "subject")
TYPE_RE = re.compile(r"^[a-z0-9_]+\.[a-z0-9_]+\.[a-z0-9_]+$")
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9:._/@#-]{0,255}$")


class InvalidEnvelope(Exception):
    pass


def validate_envelope(env) -> None:
    if not isinstance(env, dict):
        raise InvalidEnvelope("not an object")
    extra = set(env) - set(REQUIRED)
    if extra:
        raise InvalidEnvelope("unknown field")  # names only, never values
    for k in REQUIRED:
        if k not in env:
            raise InvalidEnvelope(f"missing {k}")
    if env["specversion"] != "mctl.events/v1":
        raise InvalidEnvelope("specversion")
    if not (isinstance(env["id"], str) and ID_RE.match(env["id"])):
        raise InvalidEnvelope("id")
    if not (isinstance(env["correlation_id"], str) and ID_RE.match(env["correlation_id"])):
        raise InvalidEnvelope("correlation_id")
    if not (isinstance(env["type"], str) and TYPE_RE.match(env["type"])):
        raise InvalidEnvelope("type")
    subj = env["subject"]
    if not isinstance(subj, dict) or "kind" not in subj or len(subj) > 12:
        raise InvalidEnvelope("subject")
    for k, v in subj.items():
        if not (isinstance(v, str) and 1 <= len(v) <= 256):
            raise InvalidEnvelope("subject value")


def project(env: dict, cursor: str | None = None) -> dict:
    """Validate and project. Raises InvalidEnvelope."""
    validate_envelope(env)
    occ = {
        "eventId": env["id"],
        "name": env["type"],
        "timestamp": env["occurred_at"],
        "data": {
            "source": env["source"],
            "correlation_id": env["correlation_id"],
            "subject": dict(env["subject"]),
        },
    }
    if cursor is not None:
        occ["cursor"] = cursor
    return occ


# payloadSchema advertised by events/list: references only.
PAYLOAD_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["source", "correlation_id", "subject"],
    "properties": {
        "source": {"type": "string"},
        "correlation_id": {"type": "string"},
        "subject": {"type": "object", "additionalProperties": {"type": "string", "maxLength": 256}},
    },
}
