from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import AsyncIterator

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.security import APIKeyCookie, APIKeyHeader
from starlette.types import ASGIApp, Receive, Scope, Send
from urllib.parse import urlsplit

from synai.web.activity import ProjectActivityStore
from synai.providers.base import ModelProvider, ProviderError
from synai.providers.ollama import OllamaProvider
from synai.web.chat import WebChatError, WebChatService
from synai.web.auth import (
    AuthenticatedSession,
    AuthenticationError,
    AuthenticationService,
)
from synai.web.config import WebConfig
from synai.web.database import MetadataDatabase
from synai.web.distributed import (
    DevicePrincipal,
    DistributedError,
    DistributedRegistry,
    Enrollment,
    SUPPORTED_PROTOCOL_VERSIONS,
)
from synai.web.ownership import DataRootOwnership
from synai.web.projects import (
    ProjectRegistry,
    ProjectRegistryError,
    RegisteredProject,
)
from synai.web.schemas import (
    CsrfResponse,
    ChatCancelResponse,
    ChatConversationCreateRequest,
    ChatConversationListResponse,
    ChatConversationResponse,
    ChatTurnRequest,
    ChatTurnResponse,
    ErrorResponse,
    HealthResponse,
    LoginRequest,
    LoginResponse,
    ModelListResponse,
    ModelResponse,
    PasswordChangeRequest,
    ProjectListResponse,
    ProjectRegistrationRequest,
    ProjectResponse,
    SessionResponse,
    DeviceCredentialResponse,
    DeviceCredentialRotationResponse,
    DeviceEnrollmentRequest,
    DeviceListResponse,
    DeviceMetadataResponse,
    ExecutionTargetStatusResponse,
    LogicalProjectCreateRequest,
    LogicalProjectListResponse,
    LogicalProjectResponse,
    MemoryAssociationPreviewRequest,
    MemoryAssociationPreviewResponse,
    PairingChallengeResponse,
    ProjectActivityListResponse,
    ProjectActivityEventResponse,
    SnapshotChunkResponse,
    SnapshotListResponse,
    SnapshotResponse,
    SnapshotUploadBeginRequest,
    SnapshotUploadResponse,
    SnapshotUploadStatusResponse,
    TaskContractResponse,
    TaskListResponse,
    WorkspaceBindingCreateRequest,
    WorkspaceBindingListResponse,
    WorkspaceBindingResponse,
)
from synai.web.snapshots import SnapshotStore


_logger = logging.getLogger(__name__)
_SESSION_COOKIE = "synai_session"
_CSRF_HEADER = "x-csrf-token"
_session_cookie = APIKeyCookie(
    name=_SESSION_COOKIE, scheme_name="SessionCookie", auto_error=False,
)
_csrf_header = APIKeyHeader(
    name="X-CSRF-Token", scheme_name="CsrfToken", auto_error=False,
)
_device_credential = APIKeyHeader(
    name="X-SynAI-Device-Credential", scheme_name="DeviceCredential", auto_error=False,
)
_device_id = APIKeyHeader(name="X-SynAI-Device-ID", scheme_name="DeviceID", auto_error=False)
_device_timestamp = APIKeyHeader(
    name="X-SynAI-Device-Timestamp", scheme_name="DeviceTimestamp", auto_error=False,
)
_device_nonce = APIKeyHeader(name="X-SynAI-Device-Nonce", scheme_name="DeviceNonce", auto_error=False)
_device_signature = APIKeyHeader(
    name="X-SynAI-Device-Signature", scheme_name="DeviceSignature", auto_error=False,
)


@dataclass
class WebServices:
    config: WebConfig
    provider: ModelProvider
    database: MetadataDatabase
    ownership: DataRootOwnership
    owns_provider: bool = False
    auth: AuthenticationService | None = None
    projects: ProjectRegistry | None = None
    distributed: DistributedRegistry | None = None
    snapshots: SnapshotStore | None = None
    chat: WebChatService | None = None
    activity: ProjectActivityStore | None = None
    snapshot_cleanup_task: asyncio.Task[None] | None = None
    ready: bool = False

    async def start(self) -> None:
        self.config.validate()
        self.ownership.acquire()
        try:
            self.database.initialize()
            self.auth = AuthenticationService(
                self.database, self.config.session_lifetime_seconds,
            )
            self.auth.initialize(self.config.initial_password)
            self.projects = ProjectRegistry(self.database, self.config)
            self.distributed = DistributedRegistry(self.database)
            self.activity = ProjectActivityStore(self.database)
            self.snapshots = SnapshotStore(self.database, self.distributed)
            self.snapshots.initialize()
            self.chat = WebChatService(
                self.config.data_root, self.config.ollama_url, self.database,
                self.provider, self.distributed,
            )
            self.chat.initialize()
            self.ready = True
            self.snapshot_cleanup_task = asyncio.create_task(self._snapshot_cleanup_loop())
        except BaseException:
            self.ownership.release()
            raise

    async def close(self) -> None:
        self.ready = False
        if self.chat is not None:
            await self.chat.close()
        if self.snapshot_cleanup_task is not None:
            self.snapshot_cleanup_task.cancel()
            try:
                await self.snapshot_cleanup_task
            except asyncio.CancelledError:
                pass
            self.snapshot_cleanup_task = None
        self.ownership.release()
        close = getattr(self.provider, "close", None)
        if callable(close):
            result = close()
            if asyncio.iscoroutine(result):
                await result

    async def _snapshot_cleanup_loop(self) -> None:
        while True:
            await asyncio.sleep(60)
            assert self.snapshots is not None
            try:
                await asyncio.to_thread(self.snapshots.cleanup)
            except (OSError, sqlite3.Error, ValueError):
                _logger.exception("Distributed snapshot cleanup failed")


class RequestLimitsMiddleware:
    def __init__(self, app: ASGIApp, max_body_bytes: int, max_concurrent: int) -> None:
        self.app = app
        self.max_body_bytes = max_body_bytes
        self.semaphore = asyncio.Semaphore(max_concurrent)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        state = scope.setdefault("state", {})
        state["request_id"] = uuid.uuid4().hex
        content_length = _content_length(scope)
        if content_length == -1:
            await self._error(scope, send, 400, "invalid_request", "Invalid request.")
            return
        if content_length is not None and content_length > self.max_body_bytes:
            await self._error(scope, send, 413, "request_too_large", "Request body exceeds the configured limit.")
            return
        try:
            await asyncio.wait_for(self.semaphore.acquire(), timeout=1)
        except TimeoutError:
            await self._error(scope, send, 503, "server_busy", "Server is temporarily busy.")
            return
        try:
            messages: list[dict[str, object]] = []
            size = 0
            while True:
                message = await receive()
                if message["type"] != "http.request":
                    if message["type"] == "http.disconnect":
                        return
                    continue
                chunk = message.get("body", b"")
                if not isinstance(chunk, bytes):
                    await self._error(scope, send, 400, "invalid_request", "Invalid request.")
                    return
                size += len(chunk)
                if size > self.max_body_bytes:
                    await self._error(scope, send, 413, "request_too_large", "Request body exceeds the configured limit.")
                    return
                messages.append(message)
                if not message.get("more_body", False):
                    break
            cursor = 0

            async def replay() -> dict[str, object]:
                nonlocal cursor
                if cursor < len(messages):
                    current = messages[cursor]
                    cursor += 1
                    return current
                return {"type": "http.disconnect"}

            await self.app(scope, replay, send)
        finally:
            self.semaphore.release()

    @staticmethod
    async def _error(
        scope: Scope, send: Send, status: int, code: str, message: str,
    ) -> None:
        request_id = scope.get("state", {}).get("request_id", uuid.uuid4().hex)
        response = JSONResponse(
            status_code=status,
            content={"error": {"code": code, "message": message, "request_id": request_id}},
        )
        await response(scope, _unused_receive, send)


async def _unused_receive() -> dict[str, object]:
    return {"type": "http.disconnect"}


async def _reject_websocket_input(websocket: WebSocket) -> None:
    while True:
        message = await websocket.receive()
        if message["type"] == "websocket.disconnect":
            return
        data = message.get("text")
        if data is None:
            data = message.get("bytes", b"")
        size = len(data.encode("utf-8")) if isinstance(data, str) else len(data)
        await websocket.close(code=1009 if size > 8192 else 1008)
        return


def create_app(
    config: WebConfig | None = None,
    *,
    provider: ModelProvider | None = None,
    services: WebServices | None = None,
) -> FastAPI:
    selected_config = config or WebConfig.from_env()
    selected_config.validate()
    if services is not None:
        if config is not None and services.config != config:
            raise ValueError("Injected service configuration does not match the app configuration")
        service = services
    else:
        active_provider = provider or OllamaProvider(selected_config.ollama_url)
        database = MetadataDatabase(selected_config.data_root)
        service = WebServices(
            selected_config,
            active_provider,
            database,
            DataRootOwnership(selected_config.data_root, "web"),
            owns_provider=provider is None,
        )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        try:
            await service.start()
            yield
        finally:
            await service.close()

    app = FastAPI(
        title="SynAI Web API",
        version="1.0.0",
        description=(
            "Single-operator orchestration metadata API with bounded immutable source uploads; "
            "distributed task execution and host workspace mutation are disabled."
        ),
        openapi_url="/api/openapi.json",
        docs_url="/api/docs",
        redoc_url=None,
        lifespan=lifespan,
    )
    app.state.services = service
    app.add_middleware(
        RequestLimitsMiddleware,
        max_body_bytes=selected_config.request_limit_bytes,
        max_concurrent=selected_config.concurrent_request_limit,
    )
    app.add_exception_handler(AuthenticationError, _authentication_error)
    app.add_exception_handler(WebChatError, _chat_error)
    app.add_exception_handler(ProjectRegistryError, _project_error)
    app.add_exception_handler(DistributedError, _distributed_error)
    app.add_exception_handler(RequestValidationError, _validation_error)
    app.add_exception_handler(HTTPException, _http_error)
    app.add_exception_handler(Exception, _internal_error)

    router = APIRouter()

    def services_from(request: Request) -> WebServices:
        current: WebServices = request.app.state.services
        if not current.ready:
            raise HTTPException(status_code=503, detail="not_ready")
        return current

    def current_session(
        raw_token: str | None = Depends(_session_cookie),
        current: WebServices = Depends(services_from),
    ) -> AuthenticatedSession:
        assert current.auth is not None
        return current.auth.session(raw_token)

    async def current_device(
        request: Request,
        credential: str | None = Depends(_device_credential),
        device_id: str | None = Depends(_device_id),
        timestamp: str | None = Depends(_device_timestamp),
        nonce: str | None = Depends(_device_nonce),
        signature: str | None = Depends(_device_signature),
        current: WebServices = Depends(services_from),
    ) -> DevicePrincipal:
        assert current.distributed is not None
        return current.distributed.authenticate_device(
            credential, device_id, timestamp, nonce, signature,
            request.method, request.url.path, await request.body(),
        )

    def require_origin(request: Request) -> None:
        if request.headers.get("origin") != _normalized_origin(selected_config.public_origin):
            raise AuthenticationError("origin_rejected", 403, "Request origin is not allowed.")

    def require_csrf(
        request: Request,
        session: AuthenticatedSession,
        current: WebServices,
        csrf_token: str | None,
    ) -> None:
        require_origin(request)
        assert current.auth is not None
        if not current.auth.verify_csrf(session, csrf_token):
            raise AuthenticationError("csrf_rejected", 403, "Request could not be verified.")

    @router.get("/api/v1/health", response_model=HealthResponse)
    async def health() -> HealthResponse:
        return HealthResponse(status="alive")

    @router.get("/api/v1/ready", response_model=HealthResponse)
    async def ready(current: WebServices = Depends(services_from)) -> HealthResponse:
        del current
        return HealthResponse(status="ready")

    @router.post(
        "/api/v1/auth/login",
        response_model=LoginResponse,
        responses={401: {"model": ErrorResponse}, 403: {"model": ErrorResponse}, 429: {"model": ErrorResponse}},
    )
    async def login(body: LoginRequest, request: Request, response: Response,
                    current: WebServices = Depends(services_from)) -> LoginResponse:
        require_origin(request)
        assert current.auth is not None
        peer = request.client.host if request.client is not None else "unknown"
        issued = await asyncio.to_thread(
            current.auth.login, body.password, peer,
        )
        response.set_cookie(
            _SESSION_COOKIE,
            issued.token,
            httponly=True,
            secure=selected_config.secure_cookies,
            samesite="strict",
            path="/api/v1",
            max_age=selected_config.session_lifetime_seconds,
        )
        response.headers["Cache-Control"] = "no-store"
        return LoginResponse(
            authenticated=True, csrf_token=issued.csrf_token, expires_at=issued.expires_at,
        )

    @router.get(
        "/api/v1/auth/session",
        response_model=SessionResponse,
        responses={401: {"model": ErrorResponse}},
    )
    async def session_status(
        response: Response,
        session: AuthenticatedSession = Depends(current_session),
    ) -> SessionResponse:
        response.headers["Cache-Control"] = "no-store"
        return SessionResponse(authenticated=True, expires_at=session.expires_at)

    @router.post(
        "/api/v1/auth/csrf",
        response_model=CsrfResponse,
        responses={401: {"model": ErrorResponse}, 403: {"model": ErrorResponse}},
    )
    async def csrf(
        request: Request,
        response: Response,
        session: AuthenticatedSession = Depends(current_session),
        current: WebServices = Depends(services_from),
    ) -> CsrfResponse:
        require_origin(request)
        assert current.auth is not None
        response.headers["Cache-Control"] = "no-store"
        return CsrfResponse(csrf_token=current.auth.rotate_csrf(session))

    @router.post(
        "/api/v1/auth/logout",
        response_model=SessionResponse,
        responses={401: {"model": ErrorResponse}, 403: {"model": ErrorResponse}},
    )
    async def logout(
        request: Request,
        response: Response,
        session: AuthenticatedSession = Depends(current_session),
        current: WebServices = Depends(services_from),
        csrf_token: str | None = Depends(_csrf_header),
    ) -> SessionResponse:
        require_csrf(request, session, current, csrf_token)
        assert current.auth is not None
        current.auth.logout(session)
        response.headers["Cache-Control"] = "no-store"
        response.delete_cookie(
            _SESSION_COOKIE, path="/api/v1", secure=selected_config.secure_cookies,
            httponly=True, samesite="strict",
        )
        return SessionResponse(authenticated=False, expires_at=int(time.time()))

    @router.post(
        "/api/v1/auth/password",
        status_code=204,
        responses={401: {"model": ErrorResponse}, 403: {"model": ErrorResponse}, 422: {"model": ErrorResponse}},
    )
    async def change_password(
        body: PasswordChangeRequest,
        request: Request,
        response: Response,
        session: AuthenticatedSession = Depends(current_session),
        current: WebServices = Depends(services_from),
        csrf_token: str | None = Depends(_csrf_header),
    ) -> Response:
        require_csrf(request, session, current, csrf_token)
        assert current.auth is not None
        await asyncio.to_thread(
            current.auth.change_password,
            session,
            body.current_password,
            body.new_password,
        )
        response.delete_cookie(
            _SESSION_COOKIE, path="/api/v1", secure=selected_config.secure_cookies,
            httponly=True, samesite="strict",
        )
        response.headers["Cache-Control"] = "no-store"
        response.status_code = 204
        return response

    @router.get(
        "/api/v1/models",
        response_model=ModelListResponse,
        responses={401: {"model": ErrorResponse}, 503: {"model": ErrorResponse}},
    )
    async def models(
        _: AuthenticatedSession = Depends(current_session),
        current: WebServices = Depends(services_from),
    ) -> ModelListResponse:
        try:
            available = await current.provider.list_models()
        except ProviderError as exc:
            _logger.info("Ollama model discovery unavailable")
            raise HTTPException(status_code=503, detail="provider_unavailable") from exc
        if len(available) > 4096 or any(
            not isinstance(item.name, str) or not item.name or len(item.name) > 256
            for item in available
        ):
            raise HTTPException(status_code=503, detail="provider_unavailable")
        return ModelListResponse(models=[ModelResponse(name=item.name) for item in available])

    @router.post(
        "/api/v1/chat/sessions",
        response_model=ChatConversationResponse,
        status_code=201,
        responses={401: {"model": ErrorResponse}, 403: {"model": ErrorResponse}},
    )
    async def create_chat_session(
        body: ChatConversationCreateRequest,
        request: Request,
        session: AuthenticatedSession = Depends(current_session),
        current: WebServices = Depends(services_from),
        csrf_token: str | None = Depends(_csrf_header),
    ) -> ChatConversationResponse:
        require_csrf(request, session, current, csrf_token)
        assert current.chat is not None
        return ChatConversationResponse(**await current.chat.create_session(body.model, body.project_id))

    @router.get(
        "/api/v1/chat/sessions",
        response_model=ChatConversationListResponse,
    )
    async def list_chat_sessions(
        project_id: str | None = None,
        limit: int = Query(default=100, ge=1, le=256),
        _: AuthenticatedSession = Depends(current_session),
        current: WebServices = Depends(services_from),
    ) -> ChatConversationListResponse:
        assert current.chat is not None
        return ChatConversationListResponse(
            conversations=[
                ChatConversationResponse(**entry)
                for entry in current.chat.list_sessions(project_id, limit)
            ],
        )

    @router.get(
        "/api/v1/chat/sessions/{conversation_id}",
        response_model=ChatConversationResponse,
        responses={404: {"model": ErrorResponse}},
    )
    async def get_chat_session(
        conversation_id: str,
        _: AuthenticatedSession = Depends(current_session),
        current: WebServices = Depends(services_from),
    ) -> ChatConversationResponse:
        assert current.chat is not None
        return ChatConversationResponse(**current.chat.get_session(conversation_id))

    @router.post(
        "/api/v1/chat/sessions/{conversation_id}/turns",
        response_model=ChatTurnResponse,
        status_code=202,
        responses={401: {"model": ErrorResponse}, 403: {"model": ErrorResponse}, 409: {"model": ErrorResponse}, 503: {"model": ErrorResponse}},
    )
    async def start_chat_turn(
        conversation_id: str,
        body: ChatTurnRequest,
        request: Request,
        session: AuthenticatedSession = Depends(current_session),
        current: WebServices = Depends(services_from),
        csrf_token: str | None = Depends(_csrf_header),
    ) -> ChatTurnResponse:
        require_csrf(request, session, current, csrf_token)
        assert current.chat is not None
        return ChatTurnResponse(**await current.chat.start_turn(
            conversation_id, body.prompt, body.model,
        ))

    @router.post(
        "/api/v1/chat/sessions/{conversation_id}/cancel",
        response_model=ChatCancelResponse,
        responses={401: {"model": ErrorResponse}, 403: {"model": ErrorResponse}, 404: {"model": ErrorResponse}},
    )
    async def cancel_chat_turn(
        conversation_id: str,
        request: Request,
        session: AuthenticatedSession = Depends(current_session),
        current: WebServices = Depends(services_from),
        csrf_token: str | None = Depends(_csrf_header),
    ) -> ChatCancelResponse:
        require_csrf(request, session, current, csrf_token)
        assert current.chat is not None
        return ChatCancelResponse(**await current.chat.cancel_turn(conversation_id))

    @router.websocket("/api/v1/events/v1/chat/{conversation_id}")
    async def chat_event_stream(
        websocket: WebSocket,
        conversation_id: str,
        after: int | None = Query(default=None, ge=0),
    ) -> None:
        current: WebServices = websocket.app.state.services
        if not current.ready or current.chat is None or current.auth is None:
            await websocket.close(code=1013)
            return
        if websocket.headers.get("origin") != _normalized_origin(selected_config.public_origin):
            await websocket.close(code=4403)
            return
        raw_token = websocket.cookies.get(_SESSION_COOKIE)
        try:
            current.auth.session(raw_token)
            queue, replay, cursor, gap = current.chat.subscribe(conversation_id, after)
        except AuthenticationError:
            await websocket.close(code=4401)
            return
        except WebChatError as exc:
            await websocket.close(code=4404 if exc.status_code == 404 else 4409)
            return

        await websocket.accept()
        reader = asyncio.create_task(_reject_websocket_input(websocket))
        try:
            if after is None:
                current.auth.session(raw_token)
                await websocket.send_json(current.chat.snapshot_event(conversation_id, cursor))
            elif gap:
                current.auth.session(raw_token)
                await websocket.send_json({
                    "schema_version": 1,
                    "event_id": cursor,
                    "conversation_id": conversation_id,
                    "type": "resynchronization_required",
                    "created_at": int(time.time()),
                    "payload": {"cursor": cursor},
                })
                current.auth.session(raw_token)
                await websocket.send_json(current.chat.snapshot_event(conversation_id, cursor))
            else:
                for event in replay:
                    current.auth.session(raw_token)
                    await websocket.send_json(event)
            while not reader.done():
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15)
                except TimeoutError:
                    current.auth.session(raw_token)
                    continue
                current.auth.session(raw_token)
                await websocket.send_json(event)
                current.auth.session(raw_token)
        except AuthenticationError:
            await websocket.close(code=4401)
        except WebSocketDisconnect:
            pass
        finally:
            current.chat.unsubscribe(conversation_id, queue)
            if not reader.done():
                reader.cancel()
            await asyncio.gather(reader, return_exceptions=True)

    @router.get(
        "/api/v1/logical-projects/{project_id}/activity",
        response_model=ProjectActivityListResponse,
    )
    async def project_activity(
        project_id: str,
        limit: int = Query(default=100, ge=1, le=256),
        _: AuthenticatedSession = Depends(current_session),
        current: WebServices = Depends(services_from),
    ) -> ProjectActivityListResponse:
        assert current.distributed is not None and current.activity is not None
        try:
            current.distributed.get_project(project_id)
        except DistributedError:
            raise
        events, cursor = current.activity.list_recent(project_id, limit)
        return ProjectActivityListResponse(
            project_id=project_id,
            cursor=cursor,
            events=[ProjectActivityEventResponse(**event) for event in events],
        )

    @router.websocket("/api/v1/events/v1/projects/{project_id}")
    async def project_event_stream(
        websocket: WebSocket,
        project_id: str,
        after: int | None = Query(default=None, ge=0),
    ) -> None:
        current: WebServices = websocket.app.state.services
        if not current.ready or current.activity is None or current.distributed is None or current.auth is None:
            await websocket.close(code=1013)
            return
        if websocket.headers.get("origin") != _normalized_origin(selected_config.public_origin):
            await websocket.close(code=4403)
            return
        raw_token = websocket.cookies.get(_SESSION_COOKIE)
        try:
            current.auth.session(raw_token)
            project = current.distributed.get_project(project_id)
        except AuthenticationError:
            await websocket.close(code=4401)
            return
        except DistributedError:
            await websocket.close(code=4404)
            return
        if not current.activity.acquire(project_id):
            await websocket.close(code=4409)
            return
        try:
            await websocket.accept()
            reader = asyncio.create_task(_reject_websocket_input(websocket))

            async def send_event(event: dict[str, object]) -> None:
                current.auth.session(raw_token)
                await asyncio.wait_for(websocket.send_json(event), timeout=10)

            cursor = 0 if after is None else after
            try:
                events, current_cursor, gap = current.activity.read_after(project_id, cursor)
                if after is None:
                    cursor = current_cursor
                    await send_event(current.activity.snapshot_event(
                        project_id, cursor, project.display_name, project.status,
                    ))
                elif gap:
                    cursor = current_cursor
                    await send_event(current.activity.resync_event(project_id, cursor))
                    await send_event(current.activity.snapshot_event(
                        project_id, cursor, project.display_name, project.status,
                    ))
                else:
                    for event in events:
                        await send_event(event)
                        cursor = int(event["event_id"])
                while not reader.done():
                    await asyncio.sleep(current.activity.POLL_INTERVAL_SECONDS)
                    current.auth.session(raw_token)
                    events, current_cursor, gap = current.activity.read_after(project_id, cursor)
                    if gap:
                        cursor = current_cursor
                        await send_event(current.activity.resync_event(project_id, cursor))
                        project = current.distributed.get_project(project_id)
                        await send_event(current.activity.snapshot_event(
                            project_id, cursor, project.display_name, project.status,
                        ))
                        continue
                    for event in events:
                        await send_event(event)
                        cursor = int(event["event_id"])
            except AuthenticationError:
                await websocket.close(code=4401)
            except (TimeoutError, sqlite3.Error, ValueError):
                _logger.exception("Project activity stream failed project_id=%s", project_id)
                await websocket.close(code=1013)
            except WebSocketDisconnect:
                pass
            finally:
                if not reader.done():
                    reader.cancel()
                await asyncio.gather(reader, return_exceptions=True)
        finally:
            current.activity.release(project_id)

    @router.get(
        "/api/v1/projects",
        response_model=ProjectListResponse,
        responses={401: {"model": ErrorResponse}},
    )
    async def projects(
        _: AuthenticatedSession = Depends(current_session),
        current: WebServices = Depends(services_from),
    ) -> ProjectListResponse:
        assert current.projects is not None
        return ProjectListResponse(projects=[
            _project_response(project) for project in current.projects.list_projects()
        ])

    @router.post(
        "/api/v1/projects",
        response_model=ProjectResponse,
        status_code=201,
        responses={403: {"model": ErrorResponse}, 404: {"model": ErrorResponse}, 409: {"model": ErrorResponse}},
    )
    async def register_project(
        body: ProjectRegistrationRequest,
        request: Request,
        session: AuthenticatedSession = Depends(current_session),
        current: WebServices = Depends(services_from),
        csrf_token: str | None = Depends(_csrf_header),
    ) -> ProjectResponse:
        require_csrf(request, session, current, csrf_token)
        assert current.projects is not None
        return _project_response(current.projects.register(body.workspace_key))

    @router.get(
        "/api/v1/projects/{project_id}",
        response_model=ProjectResponse,
        responses={401: {"model": ErrorResponse}, 404: {"model": ErrorResponse}},
    )
    async def inspect_project(
        project_id: str,
        _: AuthenticatedSession = Depends(current_session),
        current: WebServices = Depends(services_from),
    ) -> ProjectResponse:
        assert current.projects is not None
        return _project_response(current.projects.inspect(project_id))

    @router.post(
        "/api/v1/devices/pairing-challenges",
        response_model=PairingChallengeResponse,
        status_code=201,
    )
    async def create_pairing_challenge(
        request: Request,
        response: Response,
        session: AuthenticatedSession = Depends(current_session),
        current: WebServices = Depends(services_from),
        csrf_token: str | None = Depends(_csrf_header),
    ) -> PairingChallengeResponse:
        require_csrf(request, session, current, csrf_token)
        assert current.distributed is not None
        challenge = current.distributed.create_pairing_challenge()
        response.headers["Cache-Control"] = "no-store"
        return PairingChallengeResponse(
            challenge_id=challenge.challenge_id,
            challenge_secret=challenge.secret,
            expires_at=challenge.expires_at,
            protocol_versions=list(challenge.protocol_versions),
        )

    @router.post(
        "/api/v1/device-enrollments",
        response_model=DeviceCredentialResponse,
        status_code=201,
        responses={401: {"model": ErrorResponse}, 410: {"model": ErrorResponse}},
    )
    async def enroll_device(
        body: DeviceEnrollmentRequest,
        response: Response,
        current: WebServices = Depends(services_from),
    ) -> DeviceCredentialResponse:
        assert current.distributed is not None
        result = current.distributed.enroll(
            Enrollment(**body.model_dump()),
        )
        response.headers["Cache-Control"] = "no-store"
        return DeviceCredentialResponse(
            device_id=result.device_id,
            credential=result.credential,
            credential_expires_at=result.credential_expires_at,
            state=result.state,
        )

    @router.get("/api/v1/devices", response_model=DeviceListResponse)
    async def list_devices(
        _: AuthenticatedSession = Depends(current_session),
        current: WebServices = Depends(services_from),
    ) -> DeviceListResponse:
        assert current.distributed is not None
        return DeviceListResponse(devices=[
            DeviceMetadataResponse(**entry) for entry in current.distributed.list_devices()
        ])

    @router.get(
        "/api/v1/devices/{device_id}/capabilities",
        response_model=DeviceMetadataResponse,
        responses={404: {"model": ErrorResponse}},
    )
    async def device_capabilities(
        device_id: str,
        _: AuthenticatedSession = Depends(current_session),
        current: WebServices = Depends(services_from),
    ) -> DeviceMetadataResponse:
        assert current.distributed is not None
        return DeviceMetadataResponse(**current.distributed.device_metadata(device_id))

    @router.post(
        "/api/v1/devices/{device_id}/authorize",
        response_model=DeviceMetadataResponse,
        responses={403: {"model": ErrorResponse}, 409: {"model": ErrorResponse}},
    )
    async def authorize_device(
        device_id: str,
        request: Request,
        session: AuthenticatedSession = Depends(current_session),
        current: WebServices = Depends(services_from),
        csrf_token: str | None = Depends(_csrf_header),
    ) -> DeviceMetadataResponse:
        require_csrf(request, session, current, csrf_token)
        assert current.distributed is not None
        return DeviceMetadataResponse(**current.distributed.authorize_device(device_id))

    @router.delete(
        "/api/v1/devices/{device_id}",
        status_code=204,
        responses={403: {"model": ErrorResponse}, 404: {"model": ErrorResponse}},
    )
    async def revoke_device(
        device_id: str,
        request: Request,
        session: AuthenticatedSession = Depends(current_session),
        current: WebServices = Depends(services_from),
        csrf_token: str | None = Depends(_csrf_header),
    ) -> Response:
        require_csrf(request, session, current, csrf_token)
        assert current.distributed is not None
        current.distributed.revoke_device(device_id)
        return Response(status_code=204)

    @router.post(
        "/api/v1/logical-projects",
        response_model=LogicalProjectResponse,
        status_code=201,
    )
    async def create_logical_project(
        body: LogicalProjectCreateRequest,
        request: Request,
        session: AuthenticatedSession = Depends(current_session),
        current: WebServices = Depends(services_from),
        csrf_token: str | None = Depends(_csrf_header),
    ) -> LogicalProjectResponse:
        require_csrf(request, session, current, csrf_token)
        assert current.distributed is not None
        project = current.distributed.create_project(body.name, body.registration_key)
        return _logical_project_response(project)

    @router.get("/api/v1/logical-projects", response_model=LogicalProjectListResponse)
    async def list_logical_projects(
        _: AuthenticatedSession = Depends(current_session),
        current: WebServices = Depends(services_from),
    ) -> LogicalProjectListResponse:
        assert current.distributed is not None
        return LogicalProjectListResponse(projects=[
            _logical_project_response(project) for project in current.distributed.list_projects()
        ])

    @router.get(
        "/api/v1/logical-projects/{project_id}",
        response_model=LogicalProjectResponse,
        responses={404: {"model": ErrorResponse}},
    )
    async def inspect_logical_project(
        project_id: str,
        _: AuthenticatedSession = Depends(current_session),
        current: WebServices = Depends(services_from),
    ) -> LogicalProjectResponse:
        assert current.distributed is not None
        return _logical_project_response(current.distributed.get_project(project_id))

    @router.post(
        "/api/v1/logical-projects/{project_id}/bindings",
        response_model=WorkspaceBindingResponse,
        status_code=201,
        responses={403: {"model": ErrorResponse}, 404: {"model": ErrorResponse}, 409: {"model": ErrorResponse}},
    )
    async def create_binding(
        project_id: str,
        body: WorkspaceBindingCreateRequest,
        request: Request,
        session: AuthenticatedSession = Depends(current_session),
        current: WebServices = Depends(services_from),
        csrf_token: str | None = Depends(_csrf_header),
    ) -> WorkspaceBindingResponse:
        require_csrf(request, session, current, csrf_token)
        assert current.distributed is not None
        binding = current.distributed.create_binding(
            project_id, body.device_id, body.name, expires_at=body.expires_at,
        )
        return _binding_response(binding)

    @router.get(
        "/api/v1/logical-projects/{project_id}/bindings",
        response_model=WorkspaceBindingListResponse,
        responses={404: {"model": ErrorResponse}},
    )
    async def list_bindings(
        project_id: str,
        _: AuthenticatedSession = Depends(current_session),
        current: WebServices = Depends(services_from),
    ) -> WorkspaceBindingListResponse:
        assert current.distributed is not None
        return WorkspaceBindingListResponse(bindings=[
            _binding_response(item) for item in current.distributed.list_bindings(project_id)
        ])

    @router.delete(
        "/api/v1/logical-projects/{project_id}/bindings/{binding_id}",
        status_code=204,
        responses={403: {"model": ErrorResponse}, 404: {"model": ErrorResponse}},
    )
    async def revoke_binding(
        project_id: str,
        binding_id: str,
        request: Request,
        session: AuthenticatedSession = Depends(current_session),
        current: WebServices = Depends(services_from),
        csrf_token: str | None = Depends(_csrf_header),
    ) -> Response:
        require_csrf(request, session, current, csrf_token)
        assert current.distributed is not None
        current.distributed.revoke_binding(project_id, binding_id)
        return Response(status_code=204)

    @router.post(
        "/api/v1/device/{device_id}/credential/rotate",
        response_model=DeviceCredentialRotationResponse,
        responses={401: {"model": ErrorResponse}},
    )
    async def rotate_device_credential(
        device_id: str,
        principal: DevicePrincipal = Depends(current_device),
        current: WebServices = Depends(services_from),
    ) -> DeviceCredentialRotationResponse:
        _require_path_device(device_id, principal)
        assert current.distributed is not None
        credential = current.distributed.rotate_device_credential(principal)
        return DeviceCredentialRotationResponse(
            credential=credential.credential,
            credential_expires_at=credential.credential_expires_at,
        )

    @router.post(
        "/api/v1/device/{device_id}/snapshot-uploads",
        response_model=SnapshotUploadResponse,
        status_code=201,
        responses={401: {"model": ErrorResponse}, 403: {"model": ErrorResponse}, 413: {"model": ErrorResponse}},
    )
    async def begin_snapshot_upload(
        device_id: str,
        body: SnapshotUploadBeginRequest,
        principal: DevicePrincipal = Depends(current_device),
        current: WebServices = Depends(services_from),
    ) -> SnapshotUploadResponse:
        _require_path_device(device_id, principal)
        assert current.snapshots is not None
        result = current.snapshots.begin(
            principal, body.project_id, body.binding_id,
            [item.model_dump() for item in body.files],
            idempotency_key=body.idempotency_key,
        )
        return SnapshotUploadResponse(**result)

    @router.get(
        "/api/v1/device/{device_id}/snapshot-uploads/{upload_id}",
        response_model=SnapshotUploadStatusResponse,
        responses={401: {"model": ErrorResponse}, 404: {"model": ErrorResponse}, 410: {"model": ErrorResponse}},
    )
    async def inspect_snapshot_upload(
        device_id: str,
        upload_id: str,
        principal: DevicePrincipal = Depends(current_device),
        current: WebServices = Depends(services_from),
    ) -> SnapshotUploadStatusResponse:
        _require_path_device(device_id, principal)
        assert current.snapshots is not None
        return SnapshotUploadStatusResponse(**current.snapshots.upload_status(principal, upload_id))

    @router.put(
        "/api/v1/device/{device_id}/snapshot-uploads/{upload_id}/files/{file_index}/chunks/{chunk_index}",
        response_model=SnapshotChunkResponse,
        responses={401: {"model": ErrorResponse}, 409: {"model": ErrorResponse}, 413: {"model": ErrorResponse}},
    )
    async def accept_snapshot_chunk(
        device_id: str,
        upload_id: str,
        file_index: int,
        chunk_index: int,
        request: Request,
        principal: DevicePrincipal = Depends(current_device),
        current: WebServices = Depends(services_from),
    ) -> SnapshotChunkResponse:
        _require_path_device(device_id, principal)
        assert current.snapshots is not None
        result = current.snapshots.accept_chunk(
            principal, upload_id, file_index, chunk_index, await request.body(),
        )
        return SnapshotChunkResponse(**result)

    @router.post(
        "/api/v1/device/{device_id}/snapshot-uploads/{upload_id}/commit",
        response_model=SnapshotResponse,
        responses={401: {"model": ErrorResponse}, 409: {"model": ErrorResponse}, 410: {"model": ErrorResponse}},
    )
    async def commit_snapshot_upload(
        device_id: str,
        upload_id: str,
        principal: DevicePrincipal = Depends(current_device),
        current: WebServices = Depends(services_from),
    ) -> SnapshotResponse:
        _require_path_device(device_id, principal)
        assert current.snapshots is not None
        return SnapshotResponse(**current.snapshots.commit(principal, upload_id))

    @router.get(
        "/api/v1/logical-projects/{project_id}/snapshots",
        response_model=SnapshotListResponse,
        responses={404: {"model": ErrorResponse}},
    )
    async def list_project_snapshots(
        project_id: str,
        _: AuthenticatedSession = Depends(current_session),
        current: WebServices = Depends(services_from),
    ) -> SnapshotListResponse:
        assert current.distributed is not None and current.snapshots is not None
        current.distributed.get_project(project_id)
        return SnapshotListResponse(snapshots=list(current.snapshots.list_snapshots(project_id)))

    @router.get(
        "/api/v1/logical-projects/{project_id}/snapshots/{snapshot_id}",
        response_model=SnapshotResponse,
        responses={404: {"model": ErrorResponse}, 410: {"model": ErrorResponse}},
    )
    async def inspect_snapshot(
        project_id: str,
        snapshot_id: str,
        _: AuthenticatedSession = Depends(current_session),
        current: WebServices = Depends(services_from),
    ) -> SnapshotResponse:
        assert current.snapshots is not None
        return SnapshotResponse(**current.snapshots.snapshot_status(project_id, snapshot_id))

    @router.get(
        "/api/v1/logical-projects/{project_id}/tasks",
        response_model=TaskListResponse,
        responses={404: {"model": ErrorResponse}},
    )
    async def list_distributed_tasks(
        project_id: str,
        _: AuthenticatedSession = Depends(current_session),
        current: WebServices = Depends(services_from),
    ) -> TaskListResponse:
        assert current.distributed is not None
        current.distributed.get_project(project_id)
        with current.database.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM distributed_tasks WHERE project_id = ? "
                "ORDER BY created_at DESC LIMIT 256",
                (project_id,),
            ).fetchall()
        tasks = [
            TaskContractResponse(
                schema_version=row["schema_version"],
                task_id=row["task_id"],
                project_id=row["project_id"],
                source_snapshot_id=row["snapshot_id"],
                source_device_id=row["device_id"],
                workspace_binding_id=row["binding_id"],
                selected_execution_target=row["execution_target"] or None,
                required_capabilities=json.loads(row["required_capabilities_json"]),
                state=row["state"],
                execution_claim=row["execution_claim"],
                lease_generation=row["fencing_generation"],
                approval_reference=row["approval_reference"],
                result_reference=row["result_reference"],
                error_reference=row["error_reference"],
            )
            for row in rows
        ]
        return TaskListResponse(tasks=tasks, execution_available=False)

    @router.get(
        "/api/v1/execution-targets",
        response_model=ExecutionTargetStatusResponse,
    )
    async def list_execution_targets(
        _: AuthenticatedSession = Depends(current_session),
    ) -> ExecutionTargetStatusResponse:
        return ExecutionTargetStatusResponse(
            targets=[], execution_available=False, broker_available=False,
        )

    @router.post(
        "/api/v1/logical-projects/{project_id}/memory-association-previews",
        response_model=MemoryAssociationPreviewResponse,
        status_code=200,
        responses={403: {"model": ErrorResponse}, 404: {"model": ErrorResponse}},
    )
    async def preview_memory_association(
        project_id: str,
        body: MemoryAssociationPreviewRequest,
        request: Request,
        session: AuthenticatedSession = Depends(current_session),
        current: WebServices = Depends(services_from),
        csrf_token: str | None = Depends(_csrf_header),
    ) -> MemoryAssociationPreviewResponse:
        require_csrf(request, session, current, csrf_token)
        assert current.distributed is not None
        result = current.distributed.authorize_memory_association_preview(
            body.legacy_identity, project_id, body.provenance,
        )
        return MemoryAssociationPreviewResponse(**result)

    app.include_router(router)
    return app


def _project_response(project: RegisteredProject) -> ProjectResponse:
    return ProjectResponse(
        id=project.project_id,
        name=project.name,
        status=project.status,
        access="read_only",
    )


def _logical_project_response(project: object) -> LogicalProjectResponse:
    return LogicalProjectResponse(
        id=str(project.project_id),
        schema_version=project.schema_version,
        name=project.display_name,
        status=project.status,
        created_at=project.created_at,
    )


def _binding_response(binding: object) -> WorkspaceBindingResponse:
    return WorkspaceBindingResponse(
        id=str(binding.binding_id),
        schema_version=binding.schema_version,
        project_id=str(binding.project_id),
        device_id=str(binding.device_id),
        name=binding.display_name,
        status=binding.status,
        created_at=binding.created_at,
        expires_at=binding.expires_at,
    )


def _require_path_device(device_id: str, principal: DevicePrincipal) -> None:
    if device_id != str(principal.device_id):
        raise DistributedError("device_identity_mismatch", "Authenticated device does not match the route.", 403)


def _normalized_origin(value: str) -> str:
    parsed = urlsplit(value)
    return f"{parsed.scheme}://{parsed.netloc}"


def _content_length(scope: Scope) -> int | None:
    values = [value for name, value in scope.get("headers", []) if name.lower() == b"content-length"]
    if not values:
        return None
    if len(values) != 1:
        return -1
    try:
        parsed = int(values[0])
    except ValueError:
        return -1
    return parsed if parsed >= 0 else -1


async def _authentication_error(_: Request, exc: AuthenticationError) -> JSONResponse:
    return _error_response(exc.status_code, exc.code, exc.public_message)


async def _chat_error(_: Request, exc: WebChatError) -> JSONResponse:
    return _error_response(exc.status_code, exc.code, exc.public_message)


async def _project_error(_: Request, exc: ProjectRegistryError) -> JSONResponse:
    return _error_response(exc.status_code, exc.code, exc.public_message)


async def _distributed_error(_: Request, exc: DistributedError) -> JSONResponse:
    return _error_response(exc.status_code, exc.code, exc.public_message)


async def _validation_error(_: Request, __: RequestValidationError) -> JSONResponse:
    return _error_response(422, "invalid_request", "Request does not match the required schema.")


async def _http_error(_: Request, exc: HTTPException) -> JSONResponse:
    known = {
        (503, "not_ready"): (503, "not_ready", "Service is not ready."),
        (503, "provider_unavailable"): (503, "provider_unavailable", "Model provider is unavailable."),
    }
    status, code, message = known.get(
        (exc.status_code, exc.detail),
        (exc.status_code, "request_rejected", "Request could not be completed."),
    )
    return _error_response(status, code, message)


async def _internal_error(request: Request, _: Exception) -> JSONResponse:
    request_id = getattr(request.state, "request_id", uuid.uuid4().hex)
    _logger.error("Unhandled API error request_id=%s", request_id)
    return _error_response(500, "internal_error", "An internal error occurred.", request_id)


def _error_response(
    status: int,
    code: str,
    message: str,
    request_id: str | None = None,
) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        headers={"Cache-Control": "no-store"},
        content={"error": {
            "code": code,
            "message": message,
            "request_id": request_id or uuid.uuid4().hex,
        }},
    )
