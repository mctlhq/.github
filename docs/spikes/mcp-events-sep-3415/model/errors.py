"""JSON-RPC and Events error codes (ext-events@17c0c64a, events.mdx)."""

from __future__ import annotations

INVALID_PARAMS = -32602
METHOD_NOT_FOUND = -32601
HEADER_MISMATCH = -32020
NOT_FOUND = -32023
FORBIDDEN = -32024
RESOURCE_EXHAUSTED = -32025
UNSUPPORTED = -32026
CALLBACK_ENDPOINT_ERROR = -32027


class RpcError(Exception):
    """A JSON-RPC error. `data` never carries secrets or event content."""

    def __init__(self, code: int, message: str, data: dict | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data or {}

    def payload(self) -> dict:
        return {"code": self.code, "message": self.message, "data": self.data}


class Crash(Exception):
    """Injected process crash. Never caught by model code."""
