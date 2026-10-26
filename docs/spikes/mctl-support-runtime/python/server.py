"""A2A 1.0 REST server for the mctl-support runtime spike (Python candidate).

    DATABASE_URL=postgresql+asyncpg://spike:spike@127.0.0.1:55432/spike_python \
    PORT=8001 REPLICA=a python server.py

REST base: /v1 (create_rest_routes(path_prefix='/v1')); card at
/.well-known/agent-card.json.  Everything protocol-shaped is the SDK's:
routes, dispatcher, request handler, task store, event stream, call context.
What is ours: the bearer backend, the trace middleware, idempotent
messageId -> taskId, and cooperative cross-replica cancel.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import time
import uuid

import uvicorn
from sqlalchemy.ext.asyncio import create_async_engine
from starlette.applications import Starlette
from starlette.authentication import (
    AuthCredentials,
    AuthenticationBackend,
    AuthenticationError,
    SimpleUser,
)
from starlette.middleware import Middleware
from starlette.middleware.authentication import AuthenticationMiddleware
from starlette.requests import HTTPConnection
from starlette.responses import JSONResponse

from a2a.server.agent_execution import SimpleRequestContextBuilder
from a2a.server.cluster import (
    ConcurrentTaskModificationError,
    DatabaseTaskEventStream,
    VersionedDatabaseTaskStore,
    VersionedEvent,
)
from a2a.server.context import ServerCallContext
from a2a.server.request_handlers import DefaultRequestHandlerV2
from a2a.server.routes import create_agent_card_routes, create_rest_routes
from a2a.types.a2a_pb2 import (
    AgentCapabilities,
    AgentCard,
    AgentInterface,
    AgentSkill,
    CancelTaskRequest,
    HTTPAuthSecurityScheme,
    Message,
    SecurityScheme,
    SendMessageRequest,
    Task,
    TaskState,
    TaskStatusUpdateEvent,
)
from a2a.utils.constants import AGENT_CARD_WELL_KNOWN_PATH
from a2a.utils.errors import TaskNotCancelableError, TaskNotFoundError

from agent import CANCEL_MARKER, SupportExecutor

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("spike.server")
trace = logging.getLogger("spike.trace")

REST_BASE = "/v1"
TOKEN_PREFIX = "spike-"
PUBLIC_PATHS = {AGENT_CARD_WELL_KNOWN_PATH}
COOP_CANCEL_WAIT_S = float(os.environ.get("COOP_CANCEL_WAIT_S", "6"))
TERMINAL = {TaskState.TASK_STATE_COMPLETED, TaskState.TASK_STATE_CANCELED, TaskState.TASK_STATE_FAILED, TaskState.TASK_STATE_REJECTED}
TASK_NS = uuid.UUID("5a2a0000-0000-4000-8000-000000000001")


# --------------------------------------------------------------------------
# auth: Starlette AuthenticationMiddleware -> request.user -> SDK StarletteUser
# --------------------------------------------------------------------------


class SpikeBearerBackend(AuthenticationBackend):
    async def authenticate(self, conn: HTTPConnection):
        if conn.url.path in PUBLIC_PATHS:
            return None
        header = conn.headers.get("authorization", "")
        scheme, _, token = header.partition(" ")
        if not header:
            raise AuthenticationError("missing bearer")
        if scheme.lower() != "bearer" or not token.startswith(TOKEN_PREFIX) or len(token) == len(TOKEN_PREFIX):
            raise AuthenticationError("foreign bearer")
        return AuthCredentials(["authenticated"]), SimpleUser(token[len(TOKEN_PREFIX):])


def on_auth_error(conn: HTTPConnection, exc: Exception) -> JSONResponse:
    return JSONResponse({"error": {"code": 401, "status": "UNAUTHENTICATED", "message": str(exc)}}, status_code=401)


# --------------------------------------------------------------------------
# OTel-style trace hook: one line per request, task id + principal
# --------------------------------------------------------------------------

_TASK_RE = re.compile(r"/tasks/([^/:]+)")


class TraceMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        path, started = scope["path"], time.perf_counter()
        m = _TASK_RE.search(path)
        info = {"task": m.group(1) if m else "-", "status": 0, "body": bytearray()}
        sniff = path.endswith("message:send")

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                info["status"] = message["status"]
            elif message["type"] == "http.response.body" and sniff:
                info["body"] += message.get("body", b"")
                if not message.get("more_body", False):
                    with contextlib.suppress(Exception):
                        info["task"] = json.loads(bytes(info["body"])).get("task", {}).get("id", "-")
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            user = scope.get("user")
            principal = user.display_name if user is not None and user.is_authenticated else "-"
            trace.info(
                "trace task=%s principal=%s method=%s path=%s status=%s ms=%.0f",
                info["task"], principal, scope["method"], path, info["status"], (time.perf_counter() - started) * 1000,
            )


# --------------------------------------------------------------------------
# request-context builder: record client-supplied contextId; derive taskId
# --------------------------------------------------------------------------


def derived_task_id(owner: str, message_id: str) -> str:
    return str(uuid.uuid5(TASK_NS, f"{owner}:{message_id}"))


class SupportContextBuilder(SimpleRequestContextBuilder):
    async def build(self, context: ServerCallContext, params: SendMessageRequest | None = None,
                    task_id: str | None = None, context_id: str | None = None, task: Task | None = None):
        context.state["client_context_id"] = context_id
        if params is not None and not task_id and not params.message.task_id and params.message.message_id:
            params.message.task_id = derived_task_id(context.user.user_name, params.message.message_id)
        return await super().build(context, params, task_id, context_id, task)


# --------------------------------------------------------------------------
# request handler: idempotent messageId + cooperative remote cancel
# --------------------------------------------------------------------------


class SupportRequestHandler(DefaultRequestHandlerV2):
    async def on_message_send(self, params: SendMessageRequest, context: ServerCallContext) -> Message | Task:
        msg = params.message
        if not msg.task_id and msg.message_id:
            stored = await self._versioned_store.get(derived_task_id(context.user.user_name, msg.message_id), context)
            if stored is not None:
                log.info("duplicate messageId=%s -> task=%s (idempotent replay)", msg.message_id, stored.task.id)
                return stored.task
        return await super().on_message_send(params, context)

    async def on_cancel_task(self, params: CancelTaskRequest, context: ServerCallContext) -> Task | None:
        task_id = params.id
        if await self._active_task_registry.get(task_id) is not None:
            return await super().on_cancel_task(params, context)  # local: ActiveTask.cancel -> executor.cancel
        stored = await self._versioned_store.get(task_id, context)
        if stored is None:
            raise TaskNotFoundError
        if stored.task.status.state in TERMINAL:
            raise TaskNotCancelableError
        # Cooperative: ask the owning replica through the shared store + event
        # stream, wait for it to write CANCELED (with its own metadata); fall
        # back to the SDK's forced write if nobody answers.
        marked = Task()
        marked.CopyFrom(stored.task)
        marked.metadata.update({CANCEL_MARKER: True})
        event = TaskStatusUpdateEvent(task_id=task_id, context_id=marked.context_id, status=marked.status,
                                      metadata={CANCEL_MARKER: True})
        try:
            version = await self._versioned_store.save(marked, event=event, prev=stored.task,
                                                       prev_version=stored.version, context=context)
        except ConcurrentTaskModificationError:
            return await super().on_cancel_task(params, context)
        if self._event_stream is not None:
            await self._event_stream.publish(task_id, VersionedEvent(event=event, version=version))
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._await_canceled(task_id, version), COOP_CANCEL_WAIT_S)
        again = await self._versioned_store.get(task_id, context)
        if again is None:
            raise TaskNotFoundError
        if again.task.status.state == TaskState.TASK_STATE_CANCELED:
            log.info("task=%s cancelled cooperatively by its owner", task_id)
            return again.task
        log.warning("task=%s owner did not answer in %.0fs; forcing CANCELED", task_id, COOP_CANCEL_WAIT_S)
        return await self._cancel_remote(task_id, again.task, again.version, context)

    async def _await_canceled(self, task_id: str, after) -> None:
        async with contextlib.aclosing(self._event_stream.subscribe(task_id, after=after)) as sub:
            async for versioned in sub:
                ev = versioned.event
                if isinstance(ev, (TaskStatusUpdateEvent, Task)) and ev.status.state == TaskState.TASK_STATE_CANCELED:
                    return


# --------------------------------------------------------------------------
# wiring
# --------------------------------------------------------------------------


def build_card(port: int) -> AgentCard:
    return AgentCard(
        name="mctl-support (python spike)",
        description="Read-only tenant support agent, runtime spike S-1",
        version="0.0.1",
        supported_interfaces=[AgentInterface(url=f"http://127.0.0.1:{port}{REST_BASE}", protocol_binding="HTTP+JSON", protocol_version="1.0")],
        capabilities=AgentCapabilities(streaming=True, push_notifications=False),
        default_input_modes=["text/plain"],
        default_output_modes=["text/plain"],
        security_schemes={"bearer": SecurityScheme(http_auth_security_scheme=HTTPAuthSecurityScheme(scheme="bearer", bearer_format="spike-<principal>"))},
        skills=[AgentSkill(id="get_service_status", name="Service status", description="Reports the status of a tenant service", tags=["support", "read-only"], examples=["status of api"])],
    )


def build_app() -> Starlette:
    port = int(os.environ.get("PORT", "8001"))
    replica = os.environ.get("REPLICA", "a")
    engine = create_async_engine(os.environ["DATABASE_URL"], pool_size=5, max_overflow=5)
    store = VersionedDatabaseTaskStore(engine, create_table=True)
    stream = DatabaseTaskEventStream(engine, create_table=True, poll_interval_s=0.25)
    card = build_card(port)
    handler = SupportRequestHandler(
        agent_executor=SupportExecutor(store, stream, replica),
        task_store=store,
        agent_card=card,
        event_stream=stream,
        request_context_builder=SupportContextBuilder(),
    )

    async def lifespan(app):
        await store.initialize()
        await stream.initialize()
        log.info("replica=%s port=%d base=%s store=postgres ready", replica, port, REST_BASE)
        yield
        await handler.aclose()
        await engine.dispose()

    routes = create_agent_card_routes(card) + create_rest_routes(handler, path_prefix=REST_BASE)
    return Starlette(
        routes=routes,
        lifespan=lifespan,
        middleware=[
            Middleware(TraceMiddleware),
            Middleware(AuthenticationMiddleware, backend=SpikeBearerBackend(), on_error=on_auth_error),
        ],
    )


app = build_app()

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8001")), log_level="warning")
