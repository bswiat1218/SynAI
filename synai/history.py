from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import replace
from pathlib import Path

from synai.config import ConversationEnvironment, Settings
from synai.models import Activity, GenerationSource, Message, Session, now
from synai.storage import ConversationStorage, checked_path


class HistoryError(Exception):
    pass


class History:
    def __init__(self, directory: Path, defaults: Settings | None = None) -> None:
        self.directory = directory
        self.defaults = defaults or Settings()

    def environment_for(self, session: Session) -> ConversationEnvironment:
        if session.environment is not None:
            return session.environment
        if not isinstance(session.workspace, str) or not Path(session.workspace).is_absolute():
            raise ValueError("Legacy workspace must be an absolute path")
        settings = self.defaults
        settings = replace(settings, execution_mode="sandbox", ollama_url=session.endpoint, **{
            key: session.limits[key] for key in ("command_timeout", "output_bytes", "tool_budget")
            if key in session.limits
        })
        environment = ConversationEnvironment.from_settings(settings, Path(session.workspace))
        environment.validate()
        return environment

    def save(self, session: Session) -> None:
        self._validate_environment(session)
        self._attribute_legacy_messages(session)
        if session.schema_version in {2, 3, 4} and session.environment is not None:
            session.set_environment(session.environment)
        session.updated_at = now()
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not re.fullmatch(r"[a-f0-9]{32}", session.session_id):
            raise HistoryError("Invalid session ID")
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=self.directory, delete=False) as handle:
                temporary = Path(handle.name)
                json.dump(session.to_dict(), handle, ensure_ascii=True, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            temporary.replace(self.directory / f"{session.session_id}.json")
        except OSError as exc:
            raise HistoryError(f"Cannot save history: {exc}") from exc
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def load(self, path: Path) -> Session:
        try:
            self.check_record_path(path)
            if path.is_symlink():
                raise ValueError("Symlink history files are not supported")
            value = json.loads(path.read_text(encoding="utf-8"))
            if type(value.get("schema_version")) is not int or value["schema_version"] not in {1, 2, 3, 4, 5}:
                raise ValueError("Unsupported history version")
            if value["schema_version"] in {4, 5} and type(value.get("managed_workspace_created")) is not bool:
                raise ValueError("Managed history requires a workspace-allocation flag")
            environment_data = value.pop("environment", None)
            environment = None
            if environment_data is not None:
                if not isinstance(environment_data, dict):
                    raise ValueError("Invalid conversation environment")
                if value["schema_version"] < 3:
                    if "execution_mode" in environment_data:
                        raise ValueError("Legacy histories cannot specify execution mode")
                    environment_data["execution_mode"] = "sandbox"
                elif "execution_mode" not in environment_data:
                    raise ValueError("Version 3 environment requires execution mode")
                if value["schema_version"] < 5:
                    endpoint = environment_data.pop("ollama_url")
                    timeout = environment_data.pop("request_timeout")
                    replace(self.defaults, ollama_url=endpoint, request_timeout=timeout).validate()
                    if endpoint != value["endpoint"]:
                        raise ValueError("Saved environment differs from endpoint")
                    value["legacy_request_timeout"] = timeout
                environment = ConversationEnvironment(**environment_data)
            messages = []
            for item in value.pop("messages"):
                source = item.pop("source", None)
                if source is not None:
                    source = GenerationSource(**source)
                    if not all(isinstance(field, str) and field for field in (
                        source.model, source.endpoint, source.provider,
                    )) or type(source.legacy) is not bool:
                        raise ValueError("Invalid generation provenance")
                    replace(self.defaults, ollama_url=source.endpoint).validate()
                elif value["schema_version"] < 5 and item.get("role") == "assistant":
                    source = GenerationSource(value["model"], value["endpoint"], legacy=True)
                messages.append(Message(**item, source=source))
            activity = [Activity(**item) for item in value.pop("activity")]
            session = Session(**value, messages=messages, activity=activity, environment=environment)
            if self.identifier(path) != session.session_id:
                raise ValueError("History ID differs from filename")
            if not all(isinstance(field, str) for field in (
                session.model, session.endpoint, session.workspace, session.title, session.state,
            )):
                raise ValueError("Invalid session metadata")
            for message in messages:
                if message.role not in {"system", "user", "assistant", "tool"} or not isinstance(message.content, str):
                    raise ValueError("Invalid conversation message")
                if not isinstance(message.thinking, str) or not isinstance(message.tool_calls, list):
                    raise ValueError("Invalid reasoning or tool-call fields")
                if any(not isinstance(call, dict) or not isinstance(call.get("function"), dict) for call in message.tool_calls):
                    raise ValueError("Invalid native tool calls")
            if any(not isinstance(entry.text, str) or not isinstance(entry.kind, str) for entry in activity):
                raise ValueError("Invalid activity entry")
            self._validate_environment(session)
        except (OSError, ValueError, TypeError, KeyError, AttributeError, HistoryError) as exc:
            raise HistoryError(f"Cannot load {path.name}: {exc}") from exc
        return session

    def check_record_path(self, path: Path) -> None:
        if path.parent.resolve() != self.directory.resolve():
            raise HistoryError("History path is outside the configured directory")

    def _validate_environment(self, session: Session) -> None:
        try:
            replace(self.defaults, ollama_url=session.endpoint).validate()
            if session.container_id is not None and (
                not isinstance(session.container_id, str)
                or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", session.container_id)
            ):
                raise ValueError("Invalid saved container identifier")
            if not isinstance(session.limits, dict):
                raise ValueError("Invalid saved limits")
            if set(session.limits) - {"command_timeout", "output_bytes", "tool_budget"}:
                raise ValueError("Unknown saved limit fields")
            replace(self.defaults, **session.limits).validate()
            if session.legacy_request_timeout is not None:
                replace(self.defaults, request_timeout=session.legacy_request_timeout).validate()
            if session.schema_version in {2, 3, 4, 5} and session.environment is None:
                raise ValueError("Version 2/3 history requires a conversation environment")
            environment = self.environment_for(session)
            environment.validate()
            if session.schema_version < 3 and environment.execution_mode != "sandbox":
                raise ValueError("Legacy histories must use sandbox mode")
            if environment.execution_mode == "host" and session.container_id is not None:
                raise ValueError("Host conversations cannot reference a container")
            if session.environment is not None:
                expected = {"command_timeout": environment.command_timeout, "output_bytes": environment.output_bytes,
                            "tool_budget": environment.tool_budget}
                if session.workspace != environment.workspace or session.limits != expected:
                    raise ValueError("Saved environment differs from workspace/limits")
        except (ValueError, TypeError, OverflowError) as exc:
            raise HistoryError(f"Invalid saved environment: {exc}") from exc

    @staticmethod
    def _attribute_legacy_messages(session: Session) -> None:
        for message in session.messages:
            if message.role == "assistant" and message.source is None:
                message.source = GenerationSource(session.model, session.endpoint, legacy=True)

    def list_paths(self) -> list[Path]:
        if not self.directory.exists():
            return []
        return sorted(self.directory.glob("*.json"), key=lambda path: path.lstat().st_mtime, reverse=True)

    def identifier(self, path: Path) -> str:
        return path.stem

    def path_for(self, identifier: str) -> Path:
        return self.directory / f"{identifier}.json"

    def delete(self, path: Path) -> None:
        if (
            path.parent.resolve() != self.directory.resolve() or path.suffix != ".json"
            or path.is_symlink()
        ):
            raise HistoryError("History path is outside the configured history files")
        try:
            path.unlink()
        except OSError as exc:
            raise HistoryError(f"Cannot delete {path.name}: {exc}") from exc


class ManagedHistory(History):
    def __init__(self, storage: ConversationStorage, defaults: Settings) -> None:
        super().__init__(storage.conversations, defaults)
        self.storage = storage

    def identifier(self, path: Path) -> str:
        return self.storage.identifier(path)

    def path_for(self, identifier: str) -> Path:
        return self.storage.record(identifier)

    def _validate_environment(self, session: Session) -> None:
        super()._validate_environment(session)
        if session.schema_version not in {4, 5} or session.environment is None:
            raise HistoryError("Managed conversation requires schema version 4/5")
        if type(session.managed_workspace_created) is not bool:
            raise HistoryError("Invalid workspace-allocation flag")
        try:
            if session.environment.execution_mode == "sandbox":
                if not session.managed_workspace_created:
                    raise ValueError("Sandbox conversation has no allocated managed workspace")
                if Path(session.workspace) != self.storage.workspace(session.session_id):
                    raise ValueError("Saved sandbox path is not its managed workspace")
        except (ValueError, OSError) as exc:
            raise HistoryError(str(exc)) from exc

    def save(self, session: Session) -> None:
        self._validate_environment(session)
        self._attribute_legacy_messages(session)
        if session.environment is not None:
            session.set_environment(session.environment)
        try:
            self.storage.create(session.session_id, workspace=False)
            folder = self.storage.folder(session.session_id)
            path = self.path_for(session.session_id)
            session.updated_at = now()
            temporary: Path | None = None
            try:
                with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=folder, delete=False) as handle:
                    temporary = Path(handle.name)
                    json.dump(session.to_dict(), handle, ensure_ascii=True, indent=2)
                    handle.flush()
                    os.fsync(handle.fileno())
                temporary.replace(path)
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
        except (ValueError, OSError) as exc:
            raise HistoryError(f"Cannot save conversation: {exc}") from exc

    def check_record_path(self, path: Path) -> None:
        try:
            identifier = self.identifier(path)
            folder = self.storage.folder(identifier)
            if not folder.is_dir() or folder.stat().st_uid != os.getuid() or folder.stat().st_mode & 0o077:
                raise ValueError("Conversation folder must be private and owned by your user")
        except (ValueError, OSError) as exc:
            raise HistoryError(f"Cannot load conversation: {exc}") from exc

    def list_paths(self) -> list[Path]:
        if not self.directory.exists():
            return []
        checked_path(self.directory)
        paths = []
        for folder in self.directory.iterdir():
            if re.fullmatch(r"[a-f0-9]{32}", folder.name):
                path = folder / "conversation.json"
                if path.exists() or path.is_symlink():
                    paths.append(path)
        return sorted(paths, key=lambda path: path.lstat().st_mtime, reverse=True)

    def delete(self, path: Path) -> None:
        try:
            self.storage.delete(self.identifier(path))
        except (ValueError, OSError) as exc:
            raise HistoryError(f"Cannot delete conversation: {exc}") from exc
