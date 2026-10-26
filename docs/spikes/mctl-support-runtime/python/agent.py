"""Deterministic fake tool loop for the mctl-support runtime spike (Python).

The loop is a real tool loop (model step -> tool call -> tool result -> model
step -> final text) driven by a scripted model, `FakeModel`, so the executor
can later be unit-tested with any scripted model.  Nothing here touches the
network.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass, field
from typing import Any

from google.protobuf.json_format import MessageToDict

from a2a.helpers.proto_helpers import new_task, new_text_artifact
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.cluster import TaskEventStream, TaskVersion, VersionedTaskStore
from a2a.server.context import ServerCallContext
from a2a.server.events import EventQueue
from a2a.server.tasks import TaskUpdater
from a2a.types.a2a_pb2 import (
    ListTasksRequest,
    Role,
    Task,
    TaskState,
    TaskStatusUpdateEvent,
)

log = logging.getLogger("spike.agent")

SLOW_SECONDS = 20.0
CANCEL_MARKER = "cancel_requested"  # task.metadata key written by a remote replica


# --------------------------------------------------------------------------
# fake model + tools
# --------------------------------------------------------------------------


@dataclass
class ToolCall:
    name: str
    args: dict[str, Any]


@dataclass
class FinalText:
    text: str


class FakeModel:
    """Scripted stand-in for the LLM. `step` is called once per loop turn."""

    def __init__(self, principal: str, turn: int) -> None:
        self.principal, self.turn = principal, turn

    def step(self, user_text: str, tool_results: list[dict[str, Any]]) -> ToolCall | FinalText:
        words = user_text.split()
        if words and words[0] in ("status", "slow"):
            svc = words[-1]
            if not tool_results:
                return ToolCall("get_service_status", {"service": svc})
            r = tool_results[-1]
            return FinalText(
                f"{r['service']} is {r['status']} ({r['replicas']} replica); "
                f"principal={self.principal}; turn {self.turn}"
            )
        return FinalText(f"turn {self.turn}; principal={self.principal}")


@dataclass
class LoopControl:
    """Per-task cancellation plumbing shared by the loop and `cancel()`."""

    task_id: str
    local_cancel: asyncio.Event = field(default_factory=asyncio.Event)
    stopped: asyncio.Event = field(default_factory=asyncio.Event)
    reason: str | None = None


async def get_service_status(service: str, *, slow: bool, ctrl: LoopControl, stream: TaskEventStream) -> dict[str, Any] | None:
    """The single fake tool. In slow mode it sleeps SLOW_SECONDS but stops as
    soon as a cancel is observed; returns None when cancelled."""
    if slow:
        reason = await _sleep_unless_cancelled(SLOW_SECONDS, ctrl, stream)
        if reason:
            ctrl.reason = reason
            return None
    return {"service": service, "status": "degraded", "replicas": 1}


async def _sleep_unless_cancelled(seconds: float, ctrl: LoopControl, stream: TaskEventStream) -> str | None:
    """Race: timer vs local cancel() vs a cancel observed on the SDK's
    cross-replica event stream (written by another replica)."""

    async def remote() -> str:
        # DatabaseTaskEventStream.subscribe polls the shared task_events log.
        async with contextlib.aclosing(stream.subscribe(ctrl.task_id, after=TaskVersion.MISSING)) as sub:
            async for versioned in sub:
                ev = versioned.event
                if isinstance(ev, TaskStatusUpdateEvent):
                    if MessageToDict(ev.metadata).get(CANCEL_MARKER):
                        return "remote-cancel-requested"
                    if ev.status.state == TaskState.TASK_STATE_CANCELED:
                        return "remote-forced-cancel"
                if isinstance(ev, Task) and ev.status.state == TaskState.TASK_STATE_CANCELED:
                    return "remote-forced-cancel"
        return "stream-ended"

    async def local() -> str:
        await ctrl.local_cancel.wait()
        return "local-cancel"

    timer = asyncio.create_task(asyncio.sleep(seconds, result=None))
    watchers = [asyncio.create_task(remote()), asyncio.create_task(local())]
    try:
        done, _ = await asyncio.wait([timer, *watchers], return_when=asyncio.FIRST_COMPLETED)
        for t in done:
            if t is not timer:
                return t.result()
        return None
    finally:
        for t in (timer, *watchers):
            t.cancel()
        await asyncio.gather(timer, *watchers, return_exceptions=True)


# --------------------------------------------------------------------------
# executor
# --------------------------------------------------------------------------


class SupportExecutor(AgentExecutor):
    """A2A AgentExecutor running the fake loop. Dependencies are injected;
    the principal is read from the SDK's call context on every request."""

    def __init__(self, store: VersionedTaskStore, stream: TaskEventStream, replica: str) -> None:
        self._store, self._stream, self._replica = store, stream, replica
        self._loops: dict[str, LoopControl] = {}

    # -- transcript restored from the shared store (owner-scoped by the SDK) --
    async def _prior_user_turns(self, context_id: str, current_task_id: str, call: ServerCallContext) -> int:
        page = await self._store.list(ListTasksRequest(context_id=context_id, page_size=100), call)
        n = 0
        for t in page.tasks:
            if t.id == current_task_id:
                continue
            n += sum(1 for m in t.history if m.role == Role.ROLE_USER)
        return n

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        call = context.call_context
        principal = call.user.user_name  # set by the Starlette auth backend -> SDK StarletteUser
        task_id, context_id = context.task_id, context.context_id
        assert task_id and context_id and context.message is not None
        updater = TaskUpdater(event_queue, task_id, context_id)
        text = context.get_user_input().strip()

        if context.current_task is None:
            # First event of a new task must be the Task itself (SDK rule, active_task.py:303-310).
            await event_queue.enqueue_event(
                new_task(task_id, context_id, TaskState.TASK_STATE_SUBMITTED, history=[context.message])
            )

        prior = await self._prior_user_turns(context_id, task_id, call)
        if call.state.get("client_context_id") and prior == 0:
            # The caller named a context this principal has no tasks in: either
            # it does not exist or it belongs to someone else. Same answer either way.
            await updater.reject(updater.new_agent_message([_text("unknown context")]))
            return
        turn = prior + 1
        log.info("replica=%s task=%s principal=%s turn=%d text=%r", self._replica, task_id, principal, turn, text)

        ctrl = LoopControl(task_id)
        self._loops[task_id] = ctrl
        try:
            await updater.start_work()
            model = FakeModel(principal, turn)
            results: list[dict[str, Any]] = []
            slow = text.startswith("slow")
            while True:  # the tool loop
                step = model.step(text, results)
                if isinstance(step, FinalText):
                    await updater.add_artifact([_text(step.text)], name="answer")
                    await updater.complete()
                    return
                if step.name != "get_service_status":
                    raise RuntimeError(f"unknown tool {step.name}")
                result = await get_service_status(step.args["service"], slow=slow, ctrl=ctrl, stream=self._stream)
                if result is None:  # cancelled inside the tool
                    log.info("replica=%s task=%s loop stopped: %s", self._replica, task_id, ctrl.reason)
                    await updater.update_status(
                        TaskState.TASK_STATE_CANCELED,
                        message=updater.new_agent_message([_text(f"cancelled: {ctrl.reason}")]),
                        metadata={"loop_stopped": True, "stopped_on": self._replica, "reason": ctrl.reason},
                    )
                    return
                results.append(result)
        finally:
            ctrl.stopped.set()
            self._loops.pop(task_id, None)

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        """Local cancel path (ActiveTask.cancel on the replica running the loop)."""
        task_id = context.task_id or ""
        ctrl = self._loops.get(task_id)
        if ctrl is not None:
            ctrl.local_cancel.set()
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(ctrl.stopped.wait(), 3.0)  # the loop writes CANCELED + loop_stopped itself
            if ctrl.stopped.is_set():
                return
        task = context.current_task
        TaskUpdater_ = TaskUpdater(event_queue, task_id, task.context_id if task else context.context_id or "")
        await TaskUpdater_.update_status(TaskState.TASK_STATE_CANCELED, metadata={"loop_stopped": False})


def _text(text: str):
    from a2a.helpers.proto_helpers import new_text_part

    return new_text_part(text)
