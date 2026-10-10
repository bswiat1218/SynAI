from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from pathlib import Path
from synai.agent import Agent
from synai.config import Settings
from synai.history import HistoryError, ManagedHistory
from synai.models import Message, ModelInfo, Session
from synai.providers.base import ModelProvider, ProviderError
from synai.storage import ConversationStorage
from synai.web.database import MetadataDatabase
from synai.web.distributed import DistributedError, DistributedRegistry
from synai.web.schemas import ChatEventEnvelope


_logger = logging.getLogger(__name__)
_CONVERSATION_ID = re.compile(r"[a-f0-9]{32}\Z")


class WebChatError(Exception):
    def __init__(self, code: str, status_code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code
        self.public_message = message


class WebChatHistory(ManagedHistory):
    """Managed atomic history with no workspace or execution-environment authority."""

    def _validate_environment(self, session: Session) -> None:
        if (
            session.schema_version != 1 or session.environment is not None
            or session.workspace != "" or session.managed_workspace_created
            or session.container_id is not None or session.agent_checkpoint is not None
        ):
            raise HistoryError("Browser chat sessions cannot contain execution-target metadata")
        if not isinstance(session.model, str) or not isinstance(session.endpoint, str):
            raise HistoryError("Browser chat session metadata is invalid")
        try:
            Settings(ollama_url=session.endpoint).validate()
        except ValueError as exc:
            raise HistoryError("Browser chat provider endpoint is invalid") from exc


class WebChatService:
    MAX_SESSIONS = 256
    MAX_ACTIVE_TURNS = 8
    MAX_PROMPT_BYTES = 32 * 1024
    MAX_TRANSCRIPT_BYTES = 1024 * 1024
    MAX_MESSAGES = 256
    MAX_OUTPUT_BYTES = 512 * 1024
    MAX_SUBSCRIBERS_PER_SESSION = 16
    MAX_EVENT_BYTES = 64 * 1024
    MAX_RETAINED_EVENTS = 1024
    EVENT_DELTA_CHARS = 4096

    def __init__(
        self,
        data_root: Path,
        endpoint: str,
        database: MetadataDatabase,
        provider: ModelProvider,
        projects: DistributedRegistry,
    ) -> None:
        self.storage = ConversationStorage(data_root)
        self.history = WebChatHistory(
            self.storage, Settings(history_dir=data_root, ollama_url=endpoint),
        )
        self.endpoint = endpoint
        self.database = database
        self.provider = provider
        self.projects = projects
        self._turns: dict[str, asyncio.Task[None]] = {}
        self._turn_lock = asyncio.Lock()
        self._subscribers: dict[str, set[asyncio.Queue[dict[str, object]]]] = {}

    def initialize(self) -> None:
        self.storage.initialize()

    async def close(self) -> None:
        async with self._turn_lock:
            tasks = tuple(self._turns.values())
            for task in tasks:
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def list_models(self) -> list[ModelInfo]:
        try:
            models = await self.provider.list_models()
        except ProviderError as exc:
            raise WebChatError(
                "provider_unavailable", 503, "The model provider is unavailable.",
            ) from exc
        if len(models) > 4096 or any(
            not isinstance(model.name, str) or not model.name or len(model.name) > 256
            for model in models
        ):
            raise WebChatError(
                "provider_unavailable", 503, "The model provider returned invalid model metadata.",
            )
        return models

    async def create_session(
        self, model: str | None = None, project_id: str | None = None,
    ) -> dict[str, object]:
        if project_id is not None:
            try:
                self.projects.get_project(project_id)
            except DistributedError as exc:
                raise WebChatError("project_not_found", 404, "Project was not found.") from exc
        if model is not None:
            if not isinstance(model, str) or not model or len(model) > 256:
                raise WebChatError("model_invalid", 422, "A valid model name is required.")
            await self._require_available_model(model)
        session = Session(model or "", self.endpoint, "", schema_version=1)
        try:
            with self.database.connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                count = connection.execute(
                    "SELECT count(*) FROM web_conversations",
                ).fetchone()[0]
                if count >= self.MAX_SESSIONS:
                    raise WebChatError("session_capacity", 429, "Conversation capacity is full.")
                self.history.save(session)
                connection.execute(
                    "INSERT INTO web_conversations"
                    "(conversation_id, schema_version, project_id, title, model, state, "
                    "created_at, updated_at) VALUES (?, 1, ?, ?, ?, ?, ?, ?)",
                    (
                        session.session_id, project_id, session.title, session.model,
                        session.state, session.created_at, session.updated_at,
                    ),
                )
                connection.commit()
        except WebChatError:
            raise
        except (HistoryError, OSError, sqlite3.Error) as exc:
            raise WebChatError("conversation_persistence_failed", 500, "Conversation could not be saved.") from exc
        return self._response(session, project_id)

    def list_sessions(
        self, project_id: str | None = None, limit: int = 100,
    ) -> list[dict[str, object]]:
        if not 1 <= limit <= 256:
            raise WebChatError("invalid_limit", 422, "Conversation list limit is outside its supported range.")
        if project_id is not None:
            try:
                self.projects.get_project(project_id)
            except DistributedError as exc:
                raise WebChatError("project_not_found", 404, "Project was not found.") from exc
        with self.database.connect() as connection:
            if project_id is None:
                rows = connection.execute(
                    "SELECT conversation_id, project_id FROM web_conversations "
                    "ORDER BY updated_at DESC, conversation_id LIMIT ?",
                    (limit,),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT conversation_id, project_id FROM web_conversations "
                    "WHERE project_id = ? ORDER BY updated_at DESC, conversation_id LIMIT ?",
                    (project_id, limit),
                ).fetchall()
        return [
            self._response(self._load(row["conversation_id"]), row["project_id"], include_messages=False)
            for row in rows
        ]

    def get_session(self, conversation_id: str) -> dict[str, object]:
        row = self._conversation_row(conversation_id)
        session = self._load(conversation_id)
        return self._response(session, row["project_id"])

    def _load(self, conversation_id: str) -> Session:
        if not isinstance(conversation_id, str) or not _CONVERSATION_ID.fullmatch(conversation_id):
            raise WebChatError("conversation_not_found", 404, "Conversation was not found.")
        try:
            session = self.history.load(self.history.path_for(conversation_id))
        except (HistoryError, OSError) as exc:
            raise WebChatError(
                "conversation_unavailable", 500, "Saved conversation could not be loaded.",
            ) from exc
        if session.session_id != conversation_id:
            raise WebChatError("conversation_unavailable", 500, "Saved conversation identity is invalid.")
        if session.state == "running" and conversation_id not in self._turns:
            session.state = "interrupted"
            for message in reversed(session.messages):
                if message.role == "assistant" and message.status == "streaming":
                    message.status = "interrupted"
                    break
            self._persist(session)
            self._emit(conversation_id, "turn_interrupted", {"state": "interrupted"})
        return session

    async def start_turn(
        self, conversation_id: str, prompt: str, model: str,
    ) -> dict[str, object]:
        if not isinstance(prompt, str) or not prompt.strip():
            raise WebChatError("prompt_invalid", 422, "A non-empty prompt is required.")
        if len(prompt.encode("utf-8")) > self.MAX_PROMPT_BYTES:
            raise WebChatError("prompt_too_large", 413, "Prompt exceeds the supported size.")
        if not isinstance(model, str) or not model or len(model) > 256:
            raise WebChatError("model_invalid", 422, "A valid model name is required.")
        row = self._conversation_row(conversation_id)
        try:
            await self._require_available_model(model)
        except WebChatError as exc:
            if exc.code == "provider_unavailable":
                self._emit(conversation_id, "provider_unavailable", {"state": "unavailable"})
            raise
        session = self._load(conversation_id)
        if session.state == "running":
            raise WebChatError("turn_already_running", 409, "A conversation turn is already running.")
        if len(session.messages) + 2 > self.MAX_MESSAGES:
            raise WebChatError("transcript_limit", 413, "Conversation has reached its message limit.")
        used = sum(
            len(message.content.encode("utf-8")) + len(message.thinking.encode("utf-8"))
            for message in session.messages
        )
        if used + len(prompt.encode("utf-8")) > self.MAX_TRANSCRIPT_BYTES:
            raise WebChatError("transcript_limit", 413, "Conversation has reached its transcript limit.")
        async with self._turn_lock:
            if conversation_id in self._turns:
                raise WebChatError("turn_already_running", 409, "A conversation turn is already running.")
            if len(self._turns) >= self.MAX_ACTIVE_TURNS:
                raise WebChatError("turn_capacity", 429, "Active conversation capacity is full.")
            session.model = model
            session.state = "idle"
            self._persist(session)
            self._emit(conversation_id, "turn_started", {"model": model})
            task = asyncio.create_task(
                self._run_turn(session, prompt),
                name=f"synai-web-chat-{conversation_id}",
            )
            self._turns[conversation_id] = task
        await asyncio.sleep(0)
        return {
            "conversation_id": conversation_id,
            "model": model,
            "state": "running",
        }

    async def cancel_turn(self, conversation_id: str) -> dict[str, object]:
        self._conversation_row(conversation_id)
        async with self._turn_lock:
            task = self._turns.get(conversation_id)
            if task is None:
                session = self._load(conversation_id)
                return {"conversation_id": conversation_id, "state": session.state, "cancelled": False}
            task.cancel()
        return {"conversation_id": conversation_id, "state": "cancelling", "cancelled": True}

    async def _run_turn(self, session: Session, prompt: str) -> None:
        conversation_id = session.session_id
        offsets: dict[int, tuple[int, int]] = {}

        async def update() -> None:
            self._check_transcript_size(session)
            self._persist(session)
            assistant = next(
                (message for message in reversed(session.messages) if message.role == "assistant"),
                None,
            )
            if assistant is None:
                return
            key = id(assistant)
            content_start, thinking_start = offsets.get(key, (0, 0))
            await self._emit_chunks(
                conversation_id, "content_delta", assistant.content[content_start:],
            )
            await self._emit_chunks(
                conversation_id, "thinking_delta", assistant.thinking[thinking_start:],
            )
            offsets[key] = (len(assistant.content), len(assistant.thinking))

        agent = Agent(
            self.provider, self.history, None, update,
            tool_execution_enabled=False, max_output_bytes=self.MAX_OUTPUT_BYTES,
        )
        try:
            model_info = ModelInfo(session.model)
            await agent.turn(session, model_info, prompt)
            self._persist(session)
            if session.state == "idle":
                self._emit(conversation_id, "turn_completed", {"state": "complete"})
            else:
                self._emit(conversation_id, "turn_failed", {"state": "error"})
        except asyncio.CancelledError:
            self._persist(session)
            self._emit(conversation_id, "turn_cancelled", {"state": "cancelled"})
            raise
        except (HistoryError, OSError, ProviderError, TimeoutError, ValueError, sqlite3.Error, WebChatError):
            _logger.exception("Browser chat turn failed conversation_id=%s", conversation_id)
            session.state = "error"
            try:
                self._persist(session)
            except (HistoryError, OSError, sqlite3.Error, WebChatError):
                _logger.exception("Could not persist failed browser chat turn")
            self._emit(conversation_id, "turn_failed", {"state": "error"})
        finally:
            async with self._turn_lock:
                if self._turns.get(conversation_id) is asyncio.current_task():
                    del self._turns[conversation_id]

    async def _emit_chunks(self, conversation_id: str, event_type: str, text: str) -> None:
        for offset in range(0, len(text), self.EVENT_DELTA_CHARS):
            self._emit(
                conversation_id, event_type,
                {"text": text[offset:offset + self.EVENT_DELTA_CHARS]},
            )

    async def _require_available_model(self, model_name: str) -> None:
        available = await self.list_models()
        if model_name not in {model.name for model in available}:
            raise WebChatError("model_unavailable", 409, "Selected model is no longer available.")
        try:
            capabilities = await self.provider.capabilities(model_name)
        except ProviderError as exc:
            raise WebChatError(
                "provider_unavailable", 503, "The model provider is unavailable.",
            ) from exc
        if capabilities.chat is False:
            raise WebChatError("model_not_supported", 409, "Selected model does not support chat.")

    def _conversation_row(self, conversation_id: str):
        if not isinstance(conversation_id, str) or not _CONVERSATION_ID.fullmatch(conversation_id):
            raise WebChatError("conversation_not_found", 404, "Conversation was not found.")
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT project_id FROM web_conversations WHERE conversation_id = ?",
                (conversation_id,),
            ).fetchone()
        if row is None:
            raise WebChatError("conversation_not_found", 404, "Conversation was not found.")
        return row

    def _persist(self, session: Session) -> None:
        try:
            self.history.save(session)
            with self.database.connect() as connection:
                connection.execute(
                    "UPDATE web_conversations SET title = ?, model = ?, state = ?, updated_at = ? "
                    "WHERE conversation_id = ?",
                    (
                        session.title, session.model, session.state, session.updated_at,
                        session.session_id,
                    ),
                )
        except (HistoryError, OSError) as exc:
            raise WebChatError(
                "conversation_persistence_failed", 500, "Conversation could not be saved.",
            ) from exc

    @staticmethod
    def _check_transcript_size(session: Session) -> None:
        if len(session.messages) > WebChatService.MAX_MESSAGES:
            raise ProviderError("Conversation has reached its message limit.")
        size = sum(
            len(message.content.encode("utf-8")) + len(message.thinking.encode("utf-8"))
            for message in session.messages
        )
        if size > WebChatService.MAX_TRANSCRIPT_BYTES:
            raise ProviderError("Conversation has reached its transcript limit.")

    @staticmethod
    def _response(
        session: Session, project_id: str | None, *, include_messages: bool = True,
    ) -> dict[str, object]:
        result: dict[str, object] = {
            "schema_version": 1,
            "id": session.session_id,
            "project_id": project_id,
            "title": session.title,
            "model": session.model,
            "state": session.state,
            "created_at": session.created_at,
            "updated_at": session.updated_at,
        }
        if include_messages:
            result["messages"] = [
                {
                    "role": message.role,
                    "content": message.content,
                    "thinking": message.thinking,
                    "status": message.status,
                    "created_at": message.created_at,
                }
                for message in session.messages
                if message.role in {"user", "assistant"}
            ]
        return result

    def subscribe(
        self, conversation_id: str, after: int | None,
    ) -> tuple[asyncio.Queue[dict[str, object]], list[dict[str, object]], int, bool]:
        self._conversation_row(conversation_id)
        if after is not None and (type(after) is not int or after < 0):
            raise WebChatError("invalid_cursor", 400, "Event cursor is invalid.")
        listeners = self._subscribers.setdefault(conversation_id, set())
        if len(listeners) >= self.MAX_SUBSCRIBERS_PER_SESSION:
            raise WebChatError("subscriber_capacity", 429, "Conversation event capacity is full.")
        queue: asyncio.Queue[dict[str, object]] = asyncio.Queue(maxsize=64)
        listeners.add(queue)
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT event_sequence FROM web_conversations WHERE conversation_id = ?",
                (conversation_id,),
            ).fetchone()
            current = int(row["event_sequence"])
            first = connection.execute(
                "SELECT min(sequence) FROM web_chat_events WHERE conversation_id = ?",
                (conversation_id,),
            ).fetchone()[0]
            gap = after is not None and (
                after > current
                or after < current and (first is None or after < int(first) - 1)
            )
            if after is not None and not gap:
                events = connection.execute(
                    "SELECT event_json FROM web_chat_events WHERE conversation_id = ? "
                    "AND sequence > ? AND sequence <= ? ORDER BY sequence LIMIT 128",
                    (conversation_id, after, current),
                ).fetchall()
                replay = [json.loads(event["event_json"]) for event in events]
                if len(replay) == 128 and replay[-1]["event_id"] < current:
                    gap = True
                    replay = []
            else:
                replay = []
        return queue, replay, current, gap

    def unsubscribe(
        self, conversation_id: str, queue: asyncio.Queue[dict[str, object]],
    ) -> None:
        listeners = self._subscribers.get(conversation_id)
        if listeners is not None:
            listeners.discard(queue)
            if not listeners:
                self._subscribers.pop(conversation_id, None)

    def _emit(self, conversation_id: str, event_type: str, payload: dict[str, object]) -> None:
        try:
            with self.database.connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    "SELECT event_sequence FROM web_conversations WHERE conversation_id = ?",
                    (conversation_id,),
                ).fetchone()
                if row is None:
                    connection.rollback()
                    return
                sequence = int(row["event_sequence"]) + 1
                event = ChatEventEnvelope(
                    schema_version=1,
                    event_id=sequence,
                    conversation_id=conversation_id,
                    type=event_type,
                    created_at=int(time.time()),
                    payload=payload,
                )
                envelope = event.model_dump()
                encoded = json.dumps(envelope, ensure_ascii=False, separators=(",", ":"))
                if len(encoded.encode("utf-8")) > self.MAX_EVENT_BYTES:
                    connection.rollback()
                    raise ValueError("Chat event exceeds the supported size.")
                connection.execute(
                    "UPDATE web_conversations SET event_sequence = ? WHERE conversation_id = ?",
                    (sequence, conversation_id),
                )
                connection.execute(
                    "INSERT INTO web_chat_events(conversation_id, sequence, created_at, event_json) "
                    "VALUES (?, ?, ?, ?)",
                    (conversation_id, sequence, envelope["created_at"], encoded),
                )
                connection.execute(
                    "DELETE FROM web_chat_events WHERE conversation_id = ? AND sequence NOT IN "
                    "(SELECT sequence FROM web_chat_events WHERE conversation_id = ? "
                    "ORDER BY sequence DESC LIMIT ?)",
                    (conversation_id, conversation_id, self.MAX_RETAINED_EVENTS),
                )
                connection.commit()
        except (sqlite3.Error, ValueError, TypeError) as exc:
            _logger.exception("Could not persist chat event conversation_id=%s", conversation_id)
            raise WebChatError(
                "event_persistence_failed", 503, "Chat event state could not be persisted.",
            ) from exc
        for queue in tuple(self._subscribers.get(conversation_id, ())):
            try:
                queue.put_nowait(envelope)
            except asyncio.QueueFull:
                while not queue.empty():
                    try:
                        queue.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                queue.put_nowait(ChatEventEnvelope(
                    schema_version=1,
                    event_id=sequence,
                    conversation_id=conversation_id,
                    type="resynchronization_required",
                    created_at=int(time.time()),
                    payload={"cursor": sequence},
                ).model_dump())

    def snapshot_event(self, conversation_id: str, sequence: int) -> dict[str, object]:
        row = self._conversation_row(conversation_id)
        session = self._load(conversation_id)
        return ChatEventEnvelope(
            schema_version=1,
            event_id=sequence,
            conversation_id=conversation_id,
            type="session_snapshot",
            created_at=int(time.time()),
            payload={
                "project_id": row["project_id"],
                "title": session.title,
                "model": session.model,
                "state": session.state,
                "updated_at": session.updated_at,
            },
        ).model_dump()

    def current_cursor(self, conversation_id: str) -> int:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT event_sequence FROM web_conversations WHERE conversation_id = ?",
                (conversation_id,),
            ).fetchone()
        if row is None:
            raise WebChatError("conversation_not_found", 404, "Conversation was not found.")
        return int(row["event_sequence"])
