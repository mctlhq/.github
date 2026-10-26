"""Real-Anthropic smoke for the Python candidate (never part of the gate).

Uses the official SDK's tool runner (`client.beta.messages.tool_runner`) with
the one fake tool. Runs only when ANTHROPIC_API_KEY is set; the key is read by
the SDK from the environment and is never printed or written anywhere.
"""

from __future__ import annotations

import os
import sys


def main() -> int:
    # ANTHROPIC_AUTH_TOKEN (Bearer) is for Anthropic-compatible endpoints such
    # as Hetzner inference together with ANTHROPIC_BASE_URL; the SDK reads both.
    if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
        print("skipped: no key")
        return 0

    from anthropic import Anthropic, beta_tool

    @beta_tool
    def get_service_status(service: str) -> str:
        """Return the current status of a tenant service.

        Args:
            service: the service name, e.g. "api"
        """
        return f'{{"service": "{service}", "status": "degraded", "replicas": 1}}'

    client = Anthropic()  # key from ANTHROPIC_API_KEY or ANTHROPIC_AUTH_TOKEN
    model = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5-5")
    runner = client.beta.messages.tool_runner(
        model=model,
        max_tokens=512,
        max_iterations=4,
        tools=[get_service_status],
        system="You are a read-only tenant support agent. Use get_service_status before answering.",
        messages=[{"role": "user", "content": "status of svc-x"}],
    )
    final = None
    for message in runner:  # each item is one model turn; tool calls run in between
        final = message
        tools_used = [b.name for b in message.content if getattr(b, "type", "") == "tool_use"]
        print(f"turn stop_reason={message.stop_reason} tools={tools_used}")
    if final is None:
        print("smoke: no response")
        return 1
    text = "".join(getattr(b, "text", "") for b in final.content)
    print(f"model={model}\nfinal: {text}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
