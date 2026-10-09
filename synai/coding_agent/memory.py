from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
import stat
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Any, Generator
from uuid import uuid4

from synai.coding_agent.policies import workspace_fingerprint
from synai.coding_agent.state import (
    AgentStatus,
    ReviewOutcome,
    RepairOutcome,
    RepairStatus,
    StepStatus,
    VerificationOutcome,
    VerificationStatus,
)
from synai.intelligence.index import RepositoryIndex
from synai.storage import ConversationStorage, checked_path


MEMORY_SCHEMA_VERSION = 1
MEMORY_DB_SCHEMA_VERSION = 2
MAX_MEMORY_CANDIDATES = 4096
_DIGEST = re.compile(r"^[a-f0-9]{64}$")
_SECRET_PATTERNS = (
    re.compile(r"(?i)\b(?:api[_-]?key|access[_-]?token|password|client[_-]?secret)\s*[:=]\s*['\"]?[A-Za-z0-9_./+=-]{12,}"),
    re.compile(r"\b(?:sk-[A-Za-z0-9]{20,}|gh[pousr]_[A-Za-z0-9]{20,}|AKIA[A-Z0-9]{16})\b"),
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
)
_TERM = re.compile(r"[A-Za-z_][A-Za-z0-9_]{1,63}")
_MEMORY_STOP_WORDS = frozenset({
    "add", "also", "are", "be", "by", "change", "create", "fix", "for", "from",
    "implement", "in", "into", "is", "it", "make", "of", "on", "or", "related",
    "relevant", "test", "tests", "the", "this", "that", "to", "update", "use",
    "with", "write",
})


class MemoryCategory(StrEnum):
    ARCHITECTURE = "architecture"
    CONVENTION = "convention"
    DECISION = "decision"
    VERIFIED_OUTCOME = "verified_outcome"
    PITFALL = "pitfall"
    USER_PINNED = "user_pinned"


class MemorySource(StrEnum):
    VERIFIED_TASK = "verified_task"
    USER_PINNED = "user_pinned"


class MemoryStatus(StrEnum):
    ACTIVE = "active"
    STALE = "stale"
    SUPERSEDED = "superseded"
    ARCHIVED = "archived"


class EvidenceFreshness(StrEnum):
    CURRENT = "current"
    STALE = "stale"
    UNAVAILABLE = "unavailable"
    UNVERIFIED = "unverified"


class MemoryErrorCode(StrEnum):
    DISABLED = "memory_disabled"
    PROJECT_IDENTITY_MISMATCH = "project_identity_mismatch"
    NOT_FOUND = "memory_not_found"
    INVALID_RECORD = "invalid_memory_record"
    STORE_UNAVAILABLE = "memory_store_unavailable"
    STORE_CORRUPT = "memory_store_corrupt"
    UNSUPPORTED_SCHEMA = "unsupported_schema_version"
    CAPACITY_EXCEEDED = "memory_capacity_exceeded"
    EVIDENCE_STALE = "memory_evidence_stale"
    EVIDENCE_UNAVAILABLE = "memory_evidence_unavailable"
    CAPTURE_INELIGIBLE = "memory_capture_ineligible"
    WRITE_FAILED = "memory_write_failed"
    READ_FAILED = "memory_read_failed"
    CONFLICT = "memory_conflict"
    QUERY_LIMIT = "memory_query_limit"
    CANCELLED = "memory_cancelled"
    TIMEOUT = "memory_timeout"
    SENSITIVE_CONTENT = "memory_sensitive_content"


class ProjectMemoryError(Exception):
    def __init__(self, code: MemoryErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class ProjectMemoryConfig:
    enabled: bool = False
    automatic_capture: bool = False
    max_memories_per_project: int = 256
    max_storage_bytes: int = 16 * 1024 * 1024
    max_content_characters: int = 2_000
    max_retrieved_memories: int = 6
    max_retrieved_characters: int = 2_500
    max_retrieval_seconds: float = 1.0
    max_evidence_paths: int = 8

    def validate(self) -> None:
        if type(self.enabled) is not bool or type(self.automatic_capture) is not bool:
            raise ValueError("Project-memory switches must be booleans")
        if self.automatic_capture and not self.enabled:
            raise ValueError("Automatic project-memory capture requires memory to be enabled")
        integer_limits = (
            (self.max_memories_per_project, 1, 4096),
            (self.max_storage_bytes, 64 * 1024, 256 * 1024 * 1024),
            (self.max_content_characters, 128, 16_384),
            (self.max_retrieved_memories, 1, 32),
            (self.max_retrieved_characters, 128, 32_768),
            (self.max_evidence_paths, 1, 32),
        )
        if any(type(value) is not int or not low <= value <= high for value, low, high in integer_limits):
            raise ValueError("Project-memory limits are outside their safe bounds")
        if (
            isinstance(self.max_retrieval_seconds, bool)
            or not isinstance(self.max_retrieval_seconds, (int, float))
            or not math.isfinite(self.max_retrieval_seconds)
            or not 0.01 <= self.max_retrieval_seconds <= 10
        ):
            raise ValueError("Project-memory retrieval duration is outside its safe bounds")

    @classmethod
    def from_dict(cls, value: object) -> ProjectMemoryConfig:
        fields = set(cls.__dataclass_fields__)
        if not isinstance(value, dict) or set(value) != fields:
            raise ValueError("Invalid project-memory configuration fields")
        try:
            result = cls(**value)
            result.validate()
            return result
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Invalid project-memory configuration: {exc}") from exc


@dataclass(frozen=True)
class MemoryRecord:
    memory_id: str
    project_id: str
    category: MemoryCategory
    title: str
    content: str
    source_type: MemorySource
    source_task_id: str | None
    source_conversation_id: str | None
    source_plan_step_id: str | None
    source_verification_id: str | None
    source_review_id: str | None
    evidence_paths: tuple[str, ...]
    evidence_symbols: tuple[str, ...]
    evidence_fingerprints: dict[str, str]
    created_at: str
    updated_at: str
    last_validated_at: str | None
    confidence: float
    status: MemoryStatus
    user_pinned: bool
    capture_key: str | None = None
    schema_version: int = MEMORY_SCHEMA_VERSION

    def validate(self, limits: ProjectMemoryConfig) -> None:
        limits.validate()
        if (
            not isinstance(self.memory_id, str)
            or not re.fullmatch(r"[a-f0-9]{32}", self.memory_id)
            or not isinstance(self.project_id, str)
            or not _DIGEST.fullmatch(self.project_id)
        ):
            raise ValueError("Invalid memory or project identity")
        if not isinstance(self.category, MemoryCategory) or not isinstance(self.source_type, MemorySource):
            raise ValueError("Invalid memory category or provenance")
        if (
            not isinstance(self.title, str) or not self.title.strip()
            or len(self.title) > 160 or self.title != self.title.strip()
            or not isinstance(self.content, str) or not self.content.strip()
            or len(self.content) > limits.max_content_characters
        ):
            raise ValueError("Memory title or content is invalid or too large")
        if any(_has_sensitive_content(text) for text in (self.title, self.content)):
            raise ProjectMemoryError(
                MemoryErrorCode.SENSITIVE_CONTENT,
                "Memory content resembles a credential and was not persisted.",
            )
        for value in (
            self.source_task_id, self.source_conversation_id, self.source_plan_step_id,
            self.source_verification_id, self.source_review_id,
        ):
            if value is not None and (
                not isinstance(value, str) or not value or len(value) > 128
                or any(ord(char) < 32 for char in value)
            ):
                raise ValueError("Invalid memory provenance identifier")
        if (
            not isinstance(self.evidence_paths, tuple)
            or len(self.evidence_paths) > limits.max_evidence_paths
            or any(not _safe_relative_path(path) for path in self.evidence_paths)
            or len(set(self.evidence_paths)) != len(self.evidence_paths)
        ):
            raise ValueError("Invalid memory evidence paths")
        if (
            not isinstance(self.evidence_symbols, tuple) or len(self.evidence_symbols) > 16
            or any(not isinstance(item, str) or not item or len(item) > 160 for item in self.evidence_symbols)
        ):
            raise ValueError("Invalid memory evidence symbols")
        if (
            not isinstance(self.evidence_fingerprints, dict)
            or set(self.evidence_fingerprints) != set(self.evidence_paths)
            or any(
                not isinstance(value, str) or not _DIGEST.fullmatch(value)
                for value in self.evidence_fingerprints.values()
            )
        ):
            raise ValueError("Memory evidence fingerprints must match its source paths")
        if (
            not isinstance(self.created_at, str) or not isinstance(self.updated_at, str)
            or not _valid_timestamp(self.created_at) or not _valid_timestamp(self.updated_at)
            or self.last_validated_at is not None and not _valid_timestamp(self.last_validated_at)
        ):
            raise ValueError("Invalid memory timestamps")
        if (
            isinstance(self.confidence, bool) or not isinstance(self.confidence, (int, float))
            or not math.isfinite(self.confidence) or not 0 <= self.confidence <= 1
            or not isinstance(self.status, MemoryStatus) or type(self.user_pinned) is not bool
            or type(self.schema_version) is not int or self.schema_version != MEMORY_SCHEMA_VERSION
        ):
            raise ValueError("Invalid memory status, confidence, or schema version")
        if self.capture_key is not None and (
            not isinstance(self.capture_key, str) or not _DIGEST.fullmatch(self.capture_key)
        ):
            raise ValueError("Invalid memory capture key")
        if self.user_pinned != (self.source_type == MemorySource.USER_PINNED):
            raise ValueError("User-pinned status must match its explicit provenance")
        if self.source_type == MemorySource.VERIFIED_TASK and (
            self.category != MemoryCategory.VERIFIED_OUTCOME or not self.source_task_id
            or not self.source_verification_id or not self.source_review_id
        ):
            raise ValueError("Verified task memories require task, verification, and review provenance")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "memory_id": self.memory_id,
            "project_id": self.project_id,
            "category": self.category.value,
            "title": self.title,
            "content": self.content,
            "source_type": self.source_type.value,
            "source_task_id": self.source_task_id,
            "source_conversation_id": self.source_conversation_id,
            "source_plan_step_id": self.source_plan_step_id,
            "source_verification_id": self.source_verification_id,
            "source_review_id": self.source_review_id,
            "evidence_paths": list(self.evidence_paths),
            "evidence_symbols": list(self.evidence_symbols),
            "evidence_fingerprints": dict(self.evidence_fingerprints),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "last_validated_at": self.last_validated_at,
            "confidence": self.confidence,
            "status": self.status.value,
            "user_pinned": self.user_pinned,
            "capture_key": self.capture_key,
        }

    @classmethod
    def from_dict(cls, value: object, limits: ProjectMemoryConfig) -> MemoryRecord:
        keys = {
            "schema_version", "memory_id", "project_id", "category", "title", "content",
            "source_type", "source_task_id", "source_conversation_id",
            "source_plan_step_id", "source_verification_id", "source_review_id",
            "evidence_paths", "evidence_symbols", "evidence_fingerprints", "created_at",
            "updated_at", "last_validated_at", "confidence", "status", "user_pinned",
            "capture_key",
        }
        if not isinstance(value, dict) or set(value) != keys:
            raise ValueError("Invalid memory record fields")
        try:
            record = cls(
                **{
                    **value,
                    "category": MemoryCategory(value["category"]),
                    "source_type": MemorySource(value["source_type"]),
                    "status": MemoryStatus(value["status"]),
                    "evidence_paths": tuple(value["evidence_paths"]),
                    "evidence_symbols": tuple(value["evidence_symbols"]),
                },
            )
            record.validate(limits)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Invalid memory record: {exc}") from exc
        return record


@dataclass(frozen=True)
class RetrievedMemory:
    record: MemoryRecord
    relevance_score: int
    relevance_reasons: tuple[str, ...]
    freshness: EvidenceFreshness
    limitations: tuple[str, ...] = ()

    def validate(self, limits: ProjectMemoryConfig) -> None:
        self.record.validate(limits)
        if type(self.relevance_score) is not int or self.relevance_score < 0:
            raise ValueError("Invalid memory relevance score")
        if (
            not isinstance(self.relevance_reasons, tuple) or not self.relevance_reasons
            or any(not isinstance(item, str) or not item for item in self.relevance_reasons)
            or not isinstance(self.freshness, EvidenceFreshness)
            or not isinstance(self.limitations, tuple)
        ):
            raise ValueError("Invalid memory retrieval explanation")


@dataclass(frozen=True)
class MemoryRetrieval:
    memories: tuple[RetrievedMemory, ...] = ()
    limitations: tuple[str, ...] = ()
    truncated: bool = False
    unavailable: bool = False


def project_memory_id(workspace: Path) -> str:
    if not isinstance(workspace, Path):
        raise ProjectMemoryError(
            MemoryErrorCode.PROJECT_IDENTITY_MISMATCH,
            "Memory requires a validated workspace Path.",
        )
    try:
        canonical = workspace.resolve(strict=True)
        if (
            not canonical.is_dir() or workspace.absolute() != canonical
            or canonical in {Path("/"), Path.home().resolve()}
        ):
            raise ValueError("Workspace identity is not an allowed canonical project directory")
        info = canonical.stat()
        material = f"{os.getuid()}:{info.st_dev}:{info.st_ino}:{canonical}"
        return workspace_fingerprint(material)
    except (OSError, ValueError) as exc:
        raise ProjectMemoryError(
            MemoryErrorCode.PROJECT_IDENTITY_MISMATCH,
            f"Cannot establish a validated project identity: {exc}",
        ) from exc


class ProjectMemoryStore:
    """Private SQLite memory store; every operation derives its namespace from a workspace."""

    def __init__(
        self,
        storage_root: Path,
        config: ProjectMemoryConfig | None = None,
        *,
        busy_timeout_ms: int = 1000,
    ) -> None:
        self.config = config or ProjectMemoryConfig(enabled=True)
        self.config.validate()
        if type(busy_timeout_ms) is not int or not 1 <= busy_timeout_ms <= 10_000:
            raise ValueError("Memory database busy timeout is outside safe bounds")
        try:
            self.application_storage = ConversationStorage(storage_root)
        except (OSError, ValueError) as exc:
            raise ProjectMemoryError(MemoryErrorCode.STORE_UNAVAILABLE, str(exc)) from exc
        self.directory = checked_path(self.application_storage.root / "project-memory")
        self.path = checked_path(self.directory / "memory.sqlite3")
        self.busy_timeout_ms = busy_timeout_ms
        self._lock = threading.RLock()

    def initialize(self) -> None:
        self._ensure_directory()
        with self._connection(create=True, reading=False) as connection:
            self._ensure_schema(connection)

    def list_memories(
        self, workspace: Path, *, include_archived: bool = False, limit: int = 100,
    ) -> tuple[MemoryRecord, ...]:
        project_id = project_memory_id(workspace)
        if type(include_archived) is not bool or type(limit) is not int or not 1 <= limit <= 256:
            raise ProjectMemoryError(MemoryErrorCode.QUERY_LIMIT, "Invalid bounded memory-list request.")
        with self._connection(create=False, reading=True) as connection:
            self._ensure_schema(connection)
            rows = connection.execute(
                "SELECT record_json FROM memories WHERE project_id = ? "
                + ("" if include_archived else "AND status != 'archived' ")
                + "ORDER BY updated_at DESC, memory_id ASC LIMIT ?",
                (project_id, limit),
            ).fetchall()
        return tuple(self._decode(row[0], project_id) for row in rows)

    def get_memory(self, workspace: Path, memory_id: str) -> MemoryRecord:
        project_id = project_memory_id(workspace)
        if not isinstance(memory_id, str) or not re.fullmatch(r"[a-f0-9]{32}", memory_id):
            raise ProjectMemoryError(MemoryErrorCode.NOT_FOUND, "Memory record was not found.")
        with self._connection(create=False, reading=True) as connection:
            self._ensure_schema(connection)
            row = connection.execute(
                "SELECT record_json FROM memories WHERE project_id = ? AND memory_id = ?",
                (project_id, memory_id),
            ).fetchone()
        if row is None:
            raise ProjectMemoryError(MemoryErrorCode.NOT_FOUND, "Memory record was not found.")
        return self._decode(row[0], project_id)

    def search_memories(
        self,
        workspace: Path,
        query: str,
        *,
        limit: int = 20,
        cancellation: threading.Event | None = None,
    ) -> tuple[MemoryRecord, ...]:
        project_id = project_memory_id(workspace)
        if (
            not isinstance(query, str) or len(query) > 4096
            or type(limit) is not int or not 1 <= limit <= 64
        ):
            raise ProjectMemoryError(MemoryErrorCode.QUERY_LIMIT, "Invalid bounded memory-search request.")
        terms = _query_terms(query)
        if not terms:
            raise ProjectMemoryError(MemoryErrorCode.QUERY_LIMIT, "Invalid bounded memory-search request.")
        deadline = time.monotonic() + self.config.max_retrieval_seconds
        self._check_retrieval(cancellation, deadline)
        with self._connection(
            create=False,
            reading=True,
            timeout_ms=self._retrieval_timeout_ms(deadline),
        ) as connection:
            self._ensure_schema(connection)
            rows = self._candidate_records(
                connection, project_id, terms, (), (), cancellation, deadline,
            )
        self._check_retrieval(cancellation, deadline)
        scored = []
        for (raw,) in rows:
            self._check_retrieval(cancellation, deadline)
            record = self._decode(raw, project_id)
            matches = _token_set(f"{record.title} {record.content}") & set(terms)
            if matches:
                scored.append((len(matches), record))
        scored.sort(key=lambda item: (
            -item[0], -_timestamp_rank(item[1]), item[1].memory_id,
        ))
        return tuple(record for _, record in scored[:limit])

    def add_memory(
        self,
        workspace: Path,
        *,
        category: MemoryCategory,
        title: str,
        content: str,
        user_authorized: bool = False,
    ) -> MemoryRecord:
        self._require_user_authorization(user_authorized)
        project_id = project_memory_id(workspace)
        timestamp = _now()
        record = MemoryRecord(
            memory_id=uuid4().hex,
            project_id=project_id,
            category=category,
            title=title,
            content=content,
            source_type=MemorySource.USER_PINNED,
            source_task_id=None,
            source_conversation_id=None,
            source_plan_step_id=None,
            source_verification_id=None,
            source_review_id=None,
            evidence_paths=(),
            evidence_symbols=(),
            evidence_fingerprints={},
            created_at=timestamp,
            updated_at=timestamp,
            last_validated_at=None,
            confidence=1.0,
            status=MemoryStatus.ACTIVE,
            user_pinned=True,
        )
        if not isinstance(category, MemoryCategory):
            raise ProjectMemoryError(MemoryErrorCode.INVALID_RECORD, "Invalid memory category.")
        record.validate(self.config)
        return self._insert(record)

    def update_memory(
        self,
        workspace: Path,
        memory_id: str,
        *,
        title: str,
        content: str,
        user_authorized: bool = False,
    ) -> MemoryRecord:
        self._require_user_authorization(user_authorized)
        current, expected_record_json = self._read_memory(workspace, memory_id)
        if current.status not in {MemoryStatus.ACTIVE, MemoryStatus.STALE}:
            raise ProjectMemoryError(
                MemoryErrorCode.CONFLICT,
                "Archived or superseded memories cannot be reactivated by correction.",
            )
        updated = MemoryRecord(
            **{
                **current.__dict__,
                "category": MemoryCategory.USER_PINNED,
                "title": title,
                "content": content,
                "source_type": MemorySource.USER_PINNED,
                "status": MemoryStatus.ACTIVE,
                "evidence_paths": (),
                "evidence_symbols": (),
                "evidence_fingerprints": {},
                "updated_at": _now(),
                "last_validated_at": None,
                "confidence": 1.0,
                "user_pinned": True,
            },
        )
        updated.validate(self.config)
        self._replace(updated, workspace, expected_record_json=expected_record_json)
        return updated

    def archive_memory(
        self, workspace: Path, memory_id: str, *, user_authorized: bool = False,
    ) -> MemoryRecord:
        self._require_user_authorization(user_authorized)
        current, expected_record_json = self._read_memory(workspace, memory_id)
        if current.status not in {MemoryStatus.ACTIVE, MemoryStatus.STALE}:
            raise ProjectMemoryError(
                MemoryErrorCode.CONFLICT,
                "Only active or stale memories can be archived.",
            )
        updated = MemoryRecord(**{
            **current.__dict__, "status": MemoryStatus.ARCHIVED, "updated_at": _now(),
        })
        self._replace(updated, workspace, expected_record_json=expected_record_json)
        return updated

    def delete_memory(
        self, workspace: Path, memory_id: str, *, user_authorized: bool = False,
    ) -> None:
        self._require_user_authorization(user_authorized)
        project_id = project_memory_id(workspace)
        with self._connection(create=False, reading=False) as connection:
            self._ensure_schema(connection)
            try:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    "SELECT capture_key FROM memories WHERE project_id = ? AND memory_id = ?",
                    (project_id, memory_id),
                ).fetchone()
                if row is None:
                    raise ProjectMemoryError(MemoryErrorCode.NOT_FOUND, "Memory record was not found.")
                if row[0]:
                    tombstone_count = connection.execute(
                        "SELECT COUNT(*) FROM tombstones WHERE project_id = ?",
                        (project_id,),
                    ).fetchone()[0]
                    if tombstone_count >= self.config.max_memories_per_project * 4:
                        raise ProjectMemoryError(
                            MemoryErrorCode.CAPACITY_EXCEEDED,
                            "Project-memory deletion tombstone capacity has been reached.",
                        )
                    if self.path.stat().st_size + len(row[0]) + 8192 > self.config.max_storage_bytes:
                        raise ProjectMemoryError(
                            MemoryErrorCode.CAPACITY_EXCEEDED,
                            "Project-memory deletion tombstone would exceed the storage limit.",
                        )
                    connection.execute(
                        "INSERT OR IGNORE INTO tombstones(project_id, capture_key) VALUES (?, ?)",
                        (project_id, row[0]),
                    )
                connection.execute(
                    "DELETE FROM memories WHERE project_id = ? AND memory_id = ?",
                    (project_id, memory_id),
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    def retrieve(
        self,
        workspace: Path,
        task: str,
        repository: RepositoryIndex,
        *,
        relevant_paths: tuple[str, ...] = (),
        relevant_symbols: tuple[str, ...] = (),
        cancellation: threading.Event | None = None,
    ) -> MemoryRetrieval:
        if not self.config.enabled:
            return MemoryRetrieval(limitations=("Project memory is disabled by trusted configuration.",))
        project_id = project_memory_id(workspace)
        if (
            not isinstance(repository, RepositoryIndex) or repository.root != workspace
            or not isinstance(task, str) or not task.strip() or len(task) > 16_384
            or not isinstance(relevant_paths, tuple)
            or len(relevant_paths) > 32
            or any(not _safe_relative_path(path) for path in relevant_paths)
            or not isinstance(relevant_symbols, tuple)
            or len(relevant_symbols) > 32
            or any(not isinstance(symbol, str) or len(symbol) > 160 for symbol in relevant_symbols)
        ):
            raise ProjectMemoryError(
                MemoryErrorCode.PROJECT_IDENTITY_MISMATCH,
                "Retrieval requires the matching validated workspace and bounded task evidence.",
            )
        deadline = time.monotonic() + self.config.max_retrieval_seconds
        self._check_retrieval(cancellation, deadline)
        terms = _query_terms(task)
        try:
            with self._connection(
                create=False,
                reading=True,
                timeout_ms=self._retrieval_timeout_ms(deadline),
            ) as connection:
                self._ensure_schema(connection)
                rows = self._candidate_records(
                    connection,
                    project_id,
                    terms,
                    relevant_paths,
                    relevant_symbols,
                    cancellation,
                    deadline,
                )
            self._check_retrieval(cancellation, deadline)
        except ProjectMemoryError:
            raise
        except sqlite3.OperationalError as exc:
            raise self._database_error(exc, reading=True) from exc
        ranked: list[tuple[int, tuple[str, ...], MemoryRecord]] = []
        for (raw,) in rows:
            self._check_retrieval(cancellation, deadline)
            record = self._decode(raw, project_id)
            score, reasons = _relevance(record, terms, relevant_paths, relevant_symbols)
            if score > 0:
                ranked.append((score, reasons, record))
        ranked.sort(key=lambda item: (
            -item[0], -_timestamp_rank(item[2]), item[2].memory_id,
        ))
        truncated = len(ranked) > self.config.max_retrieved_memories
        selected = ranked[:self.config.max_retrieved_memories]
        paths = tuple(dict.fromkeys(
            path for _, _, record in selected for path in record.evidence_paths
        ))
        source_hashes: dict[str, str | None] = {}
        unavailable_paths: set[str] = set()
        if paths:
            maximum_evidence_paths = max(1, self.config.max_evidence_paths)
            if len(paths) > maximum_evidence_paths:
                unavailable_paths.update(paths[maximum_evidence_paths:])
                paths = paths[:maximum_evidence_paths]
                truncated = True
            bytes_read = 0
            for path in paths:
                self._check_retrieval(cancellation, deadline)
                if bytes_read >= 8 * 1024 * 1024:
                    unavailable_paths.add(path)
                    continue
                try:
                    current = repository.read_file_bytes(
                        path,
                        max_bytes=min(
                            repository.limits.max_file_bytes,
                            8 * 1024 * 1024 - bytes_read,
                        ),
                        cancellation=cancellation,
                    )
                except (OSError, ValueError) as exc:
                    if cancellation is not None and cancellation.is_set():
                        raise ProjectMemoryError(
                            MemoryErrorCode.CANCELLED,
                            "Memory source validation was cancelled.",
                        ) from exc
                    unavailable_paths.add(path)
                    continue
                if current is None:
                    source_hashes[path] = None
                else:
                    bytes_read += len(current)
                    source_hashes[path] = hashlib.sha256(current).hexdigest()
                self._check_retrieval(cancellation, deadline)
        retrieved: list[RetrievedMemory] = []
        total_characters = 0
        limitations: list[str] = []
        for score, reasons, record in selected:
            self._check_retrieval(cancellation, deadline)
            freshness = EvidenceFreshness.UNVERIFIED
            memory_limitations: list[str] = []
            if record.evidence_paths:
                unavailable = False
                stale = False
                for path in record.evidence_paths:
                    if path in unavailable_paths:
                        unavailable = True
                    elif path not in source_hashes:
                        unavailable = True
                    elif source_hashes[path] is None:
                        stale = True
                    elif path not in record.evidence_fingerprints:
                        unavailable = True
                    elif source_hashes[path] != record.evidence_fingerprints[path]:
                        stale = True
                if stale:
                    freshness = EvidenceFreshness.STALE
                    memory_limitations.append("Stored source fingerprint no longer matches current workspace evidence.")
                    if not self._mark_stale(project_id, record.memory_id):
                        limitations.append(
                            f"Memory {record.memory_id} changed during source validation and was omitted."
                        )
                        continue
                    limitations.append(f"Memory {record.memory_id} was excluded because its source evidence is stale.")
                    continue
                if unavailable:
                    freshness = EvidenceFreshness.UNAVAILABLE
                    memory_limitations.append("Current source evidence could not be validated.")
                    limitations.append(f"Memory {record.memory_id} source evidence is unavailable.")
                    continue
                freshness = EvidenceFreshness.CURRENT
                if not self._mark_validated(project_id, record.memory_id):
                    limitations.append(
                        f"Memory {record.memory_id} changed during source validation and was omitted."
                    )
                    continue
                record = MemoryRecord(**{
                    **record.__dict__,
                    "last_validated_at": _now(),
                })
            else:
                memory_limitations.append("No source-linked fingerprint; historical or user-pinned information only.")
            rendered_cost = _memory_context_length(record)
            if total_characters + rendered_cost > self.config.max_retrieved_characters:
                truncated = True
                continue
            total_characters += rendered_cost
            retrieved.append(RetrievedMemory(
                record,
                score + (25 if freshness == EvidenceFreshness.CURRENT else 0),
                (*reasons, *(("current_source_evidence",) if freshness == EvidenceFreshness.CURRENT else ())),
                freshness,
                tuple(memory_limitations),
            ))
        retrieved.sort(key=lambda item: (
            -item.relevance_score,
            -_timestamp_rank(item.record),
            item.record.memory_id,
        ))
        if truncated:
            limitations.append("Additional relevant memories were omitted by the retrieval limit.")
        return MemoryRetrieval(
            tuple(retrieved), tuple(dict.fromkeys(limitations)), truncated, False,
        )
    def capture_verified_task(
        self,
        workspace: Path,
        task: Any,
        conversation_id: str,
        repository: RepositoryIndex,
    ) -> MemoryRecord:
        if not self.config.enabled or not self.config.automatic_capture:
            raise ProjectMemoryError(MemoryErrorCode.DISABLED, "Automatic project-memory capture is disabled.")
        project_id = project_memory_id(workspace)
        if (
            not isinstance(repository, RepositoryIndex) or repository.root != workspace
            or getattr(task, "status", None) != AgentStatus.COMPLETED
            or getattr(task, "verification_outcome", None) != VerificationOutcome.PASSED
            or getattr(task, "review_record", None) is None
            or task.review_record.outcome not in {ReviewOutcome.PASSED, ReviewOutcome.PASSED_WITH_WARNINGS}
            or not getattr(task, "verification_plan", None)
            or getattr(task, "plan", None) is None
            or not isinstance(conversation_id, str) or not re.fullmatch(r"[a-f0-9]{32}", conversation_id)
        ):
            raise ProjectMemoryError(
                MemoryErrorCode.CAPTURE_INELIGIBLE,
                "Only a completed task with passing verification and review is eligible.",
            )
        if (
            task.review_record.task_id != task.task_id
            or task.review_record.verification_run_id != task.verification_plan.run_id
            or task.review_record.workspace_identity != str(workspace)
            or any(step.status != StepStatus.COMPLETED for step in task.plan.steps)
        ):
            raise ProjectMemoryError(
                MemoryErrorCode.CAPTURE_INELIGIBLE,
                "Review, plan, or workspace provenance does not match the completed task.",
            )
        checks = [
            result for result in task.verification_results
            if result.run_id == task.verification_plan.run_id
        ]
        expected = {
            check.check_id for check in task.verification_plan.checks
            if check.required
        }
        completed = {
            result.check_id for result in checks if result.status == VerificationStatus.PASSED
        }
        if not expected or not expected.issubset(completed):
            raise ProjectMemoryError(
                MemoryErrorCode.CAPTURE_INELIGIBLE,
                "Required verification evidence is missing or incomplete.",
            )
        task_evidence = [
            item for item in task.change_evidence if item.task_id == task.task_id
        ]
        evidence = [
            item for item in task_evidence if item.outcome in {"succeeded", "no_op"}
        ]
        if (
            not evidence
            or any(
                item.uncertain or item.after_exists is None
                or item.outcome in {"uncertain", "interrupted"}
                for item in task_evidence
            )
        ):
            raise ProjectMemoryError(
                MemoryErrorCode.CAPTURE_INELIGIBLE,
                "Complete, certain task-specific mutation evidence is required.",
            )
        by_path: dict[str, str] = {}
        repair_paths: set[str] = set()
        for item in evidence:
            if item.after_exists and item.after_hash:
                by_path[item.path] = item.after_hash
            if item.repair_attempt_id is not None:
                repair_paths.add(item.path)
        if not by_path or len(by_path) > self.config.max_evidence_paths:
            raise ProjectMemoryError(
                MemoryErrorCode.CAPTURE_INELIGIBLE,
                "Task source fingerprints are absent or exceed the bounded evidence limit.",
            )
        if any(not _DIGEST.fullmatch(value) for value in by_path.values()):
            raise ProjectMemoryError(MemoryErrorCode.CAPTURE_INELIGIBLE, "Task source fingerprint is invalid.")
        baselines = [
            item for item in task.change_baselines if item.task_id == task.task_id
        ]
        baseline_paths = {item.path for item in baselines if item.complete}
        if (
            not baselines
            or any(
                not item.complete or item.workspace_identity != str(workspace)
                for item in baselines
            )
            or any(item.path not in baseline_paths for item in evidence)
        ):
            raise ProjectMemoryError(
                MemoryErrorCode.PROJECT_IDENTITY_MISMATCH,
                "Complete task baselines do not match the validated workspace and changed paths.",
            )
        paths = tuple(sorted(by_path))
        for path in paths:
            source = repository.read_file_bytes(path)
            current_hash = hashlib.sha256(source).hexdigest() if source is not None else None
            if current_hash is None or current_hash != by_path[path]:
                raise ProjectMemoryError(
                    MemoryErrorCode.EVIDENCE_STALE,
                    f"Task source evidence changed before memory capture: {path}",
                )
        check_names = ", ".join(
            check.intent.value for check in task.verification_plan.checks if check.required
        )
        successful_repair = (
            task.repair_outcome == RepairOutcome.VERIFICATION_PASSED
            and any(attempt.status == RepairStatus.SUCCEEDED for attempt in task.repair_attempts)
            and bool(repair_paths)
        )
        repair_text = (
            " A permitted repair was completed and reverification passed."
            if successful_repair else ""
        )
        content = (
            f"Completed task: {task.goal.strip()[:700]}. "
            f"Required checks passed: {check_names[:250]}.{repair_text} "
            f"Reviewed task-attributed paths: {', '.join(paths)[:500]}. "
            "This records a historical verified outcome, not a guarantee about current behavior."
        )
        capture_material = json.dumps(
            {
                "task_id": task.task_id,
                "evidence": sorted(
                    (item.execution_id, item.path, item.after_hash, item.repair_attempt_id)
                    for item in evidence
                ),
                "verification_id": task.verification_plan.run_id,
                "review": task.review_record.outcome.value,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        capture_key = hashlib.sha256(capture_material).hexdigest()
        record = MemoryRecord(
            memory_id=uuid4().hex,
            project_id=project_id,
            category=MemoryCategory.VERIFIED_OUTCOME,
            title=f"Verified task: {task.goal.strip()[:120]}".rstrip(),
            content=content[:self.config.max_content_characters],
            source_type=MemorySource.VERIFIED_TASK,
            source_task_id=task.task_id,
            source_conversation_id=conversation_id,
            source_plan_step_id=None,
            source_verification_id=task.verification_plan.run_id,
            source_review_id=task.task_id,
            evidence_paths=paths,
            evidence_symbols=(),
            evidence_fingerprints=by_path,
            created_at=_now(),
            updated_at=_now(),
            last_validated_at=_now(),
            confidence=0.9,
            status=MemoryStatus.ACTIVE,
            user_pinned=False,
            capture_key=capture_key,
        )
        record.validate(self.config)
        return self._insert(record, idempotent=True)

    def _insert(self, record: MemoryRecord, *, idempotent: bool = False) -> MemoryRecord:
        record.validate(self.config)
        self._ensure_directory()
        with self._connection(create=True, reading=False) as connection:
            self._ensure_schema(connection)
            try:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    "INSERT OR IGNORE INTO projects(project_id, created_at) VALUES (?, ?)",
                    (record.project_id, _now()),
                )
                duplicate = None
                if record.capture_key:
                    duplicate = connection.execute(
                        "SELECT record_json FROM memories WHERE project_id = ? AND capture_key = ?",
                        (record.project_id, record.capture_key),
                    ).fetchone()
                    tombstone = connection.execute(
                        "SELECT 1 FROM tombstones WHERE project_id = ? AND capture_key = ?",
                        (record.project_id, record.capture_key),
                    ).fetchone()
                    if tombstone:
                        connection.rollback()
                        raise ProjectMemoryError(
                            MemoryErrorCode.CONFLICT,
                            "This task outcome was explicitly deleted and will not be recaptured.",
                        )
                if duplicate:
                    connection.rollback()
                    if idempotent:
                        return self._decode(duplicate[0], record.project_id)
                    raise ProjectMemoryError(MemoryErrorCode.CONFLICT, "Duplicate memory capture key.")
                count = connection.execute(
                    "SELECT COUNT(*) FROM memories WHERE project_id = ?", (record.project_id,),
                ).fetchone()[0]
                if count >= self.config.max_memories_per_project:
                    connection.rollback()
                    raise ProjectMemoryError(
                        MemoryErrorCode.CAPACITY_EXCEEDED,
                        "Project memory record limit has been reached.",
                    )
                record_json = json.dumps(
                    record.to_dict(), ensure_ascii=True, separators=(",", ":"),
                )
                estimated_bytes = self.path.stat().st_size + len(record_json.encode("utf-8")) + 8192
                if estimated_bytes > self.config.max_storage_bytes:
                    connection.rollback()
                    raise ProjectMemoryError(
                        MemoryErrorCode.CAPACITY_EXCEEDED,
                        "Project-memory storage byte limit would be exceeded.",
                    )
                connection.execute(
                    "INSERT INTO memories(memory_id, project_id, capture_key, status, updated_at, record_json) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        record.memory_id, record.project_id, record.capture_key,
                        record.status.value, record.updated_at,
                        record_json,
                    ),
                )
                self._index_record(connection, record)
                self._check_storage_size(connection)
                connection.commit()
            except ProjectMemoryError:
                connection.rollback()
                connection.rollback()
                raise
            except sqlite3.IntegrityError as exc:
                connection.rollback()
                raise ProjectMemoryError(MemoryErrorCode.CONFLICT, str(exc)) from exc
            except sqlite3.OperationalError as exc:
                connection.rollback()
                raise self._database_error(exc, reading=False) from exc
        return record

    def _replace(
        self,
        record: MemoryRecord,
        workspace: Path,
        *,
        expected_record_json: str,
    ) -> None:
        record.validate(self.config)
        project_id = project_memory_id(workspace)
        if record.project_id != project_id:
            raise ProjectMemoryError(
                MemoryErrorCode.PROJECT_IDENTITY_MISMATCH,
                "Memory record belongs to a different validated project.",
            )
        with self._connection(create=False, reading=False) as connection:
            self._ensure_schema(connection)
            try:
                connection.execute("BEGIN IMMEDIATE")
                cursor = connection.execute(
                    "UPDATE memories SET status = ?, updated_at = ?, record_json = ? "
                    "WHERE project_id = ? AND memory_id = ? AND record_json = ?",
                    (
                        record.status.value, record.updated_at,
                        json.dumps(record.to_dict(), ensure_ascii=True, separators=(",", ":")),
                        project_id, record.memory_id, expected_record_json,
                    ),
                )
                if cursor.rowcount != 1:
                    exists = connection.execute(
                        "SELECT 1 FROM memories WHERE project_id = ? AND memory_id = ?",
                        (project_id, record.memory_id),
                    ).fetchone()
                    code = MemoryErrorCode.CONFLICT if exists else MemoryErrorCode.NOT_FOUND
                    raise ProjectMemoryError(code, "Memory record changed or was not found.")
                self._index_record(connection, record)
                self._check_storage_size(connection)
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    def _mark_stale(self, project_id: str, memory_id: str) -> bool:
        return self._update_status(project_id, memory_id, MemoryStatus.STALE)

    def _mark_validated(self, project_id: str, memory_id: str) -> bool:
        with self._connection(create=False, reading=True) as connection:
            self._ensure_schema(connection)
            row = connection.execute(
                "SELECT record_json FROM memories WHERE project_id = ? AND memory_id = ? "
                "AND status = 'active'",
                (project_id, memory_id),
            ).fetchone()
            if row is None:
                return False
            raw = row[0]
            record = self._decode(raw, project_id)
            updated = MemoryRecord(**{
                **record.__dict__, "last_validated_at": _now(),
            })
            cursor = connection.execute(
                "UPDATE memories SET record_json = ? "
                "WHERE project_id = ? AND memory_id = ? AND status = 'active' AND record_json = ?",
                (
                    json.dumps(updated.to_dict(), ensure_ascii=True, separators=(",", ":")),
                    project_id, memory_id, raw,
                ),
            )
            return cursor.rowcount == 1

    def _update_status(self, project_id: str, memory_id: str, status: MemoryStatus) -> bool:
        with self._connection(create=False, reading=False) as connection:
            self._ensure_schema(connection)
            row = connection.execute(
                "SELECT record_json FROM memories WHERE project_id = ? AND memory_id = ?",
                (project_id, memory_id),
            ).fetchone()
            if row is None:
                return False
            raw = row[0]
            record = self._decode(raw, project_id)
            if record.status != MemoryStatus.ACTIVE:
                return False
            updated = MemoryRecord(**{
                **record.__dict__, "status": status, "updated_at": _now(),
            })
            cursor = connection.execute(
                "UPDATE memories SET status = ?, updated_at = ?, record_json = ? "
                "WHERE project_id = ? AND memory_id = ? AND status = 'active' AND record_json = ?",
                (
                    status.value, updated.updated_at,
                    json.dumps(updated.to_dict(), ensure_ascii=True, separators=(",", ":")),
                    project_id, memory_id, raw,
                ),
            )
            return cursor.rowcount == 1

    def _read_memory(self, workspace: Path, memory_id: str) -> tuple[MemoryRecord, str]:
        project_id = project_memory_id(workspace)
        if not isinstance(memory_id, str) or not re.fullmatch(r"[a-f0-9]{32}", memory_id):
            raise ProjectMemoryError(MemoryErrorCode.NOT_FOUND, "Memory record was not found.")
        with self._connection(create=False, reading=True) as connection:
            self._ensure_schema(connection)
            row = connection.execute(
                "SELECT record_json FROM memories WHERE project_id = ? AND memory_id = ?",
                (project_id, memory_id),
            ).fetchone()
        if row is None:
            raise ProjectMemoryError(MemoryErrorCode.NOT_FOUND, "Memory record was not found.")
        return self._decode(row[0], project_id), row[0]

    def _candidate_records(
        self,
        connection: sqlite3.Connection,
        project_id: str,
        terms: tuple[str, ...],
        paths: tuple[str, ...],
        symbols: tuple[str, ...],
        cancellation: threading.Event | None,
        deadline: float,
    ) -> list[tuple[str]]:
        query_terms = tuple(dict.fromkeys(terms))
        query_paths = tuple(dict.fromkeys(paths))
        query_filenames = tuple(dict.fromkeys(PurePosixPath(path).name for path in paths))
        query_symbols = tuple(dict.fromkeys(symbols))
        candidates: list[tuple[str]] = []
        for kind, values in (
            ("lexical", query_terms),
            ("path", query_paths),
            ("filename", query_filenames),
            ("symbol", query_symbols),
        ):
            self._check_retrieval(cancellation, deadline)
            if not values:
                continue
            placeholders = ",".join("?" for _ in values)
            sql = (
                "SELECT m.record_json FROM memories AS m JOIN ("
                "SELECT DISTINCT memory_id FROM memory_terms "
                f"WHERE project_id = ? AND kind = ? AND term IN ({placeholders})"
                ") AS matches ON matches.memory_id = m.memory_id "
                "WHERE m.project_id = ? AND m.status = 'active' "
                "ORDER BY m.updated_at DESC, m.memory_id ASC LIMIT ?"
            )
            connection.set_progress_handler(
                lambda: int(
                    cancellation is not None and cancellation.is_set()
                    or time.monotonic() > deadline
                ),
                500,
            )
            try:
                candidates.extend(connection.execute(
                    sql,
                    (project_id, kind, *values, project_id, MAX_MEMORY_CANDIDATES + 1),
                ).fetchall())
            except sqlite3.OperationalError as exc:
                if cancellation is not None and cancellation.is_set():
                    raise ProjectMemoryError(
                        MemoryErrorCode.CANCELLED,
                        "Memory candidate search was cancelled.",
                    ) from exc
                if time.monotonic() > deadline:
                    raise ProjectMemoryError(
                        MemoryErrorCode.TIMEOUT,
                        "Memory candidate search exceeded its time limit.",
                    ) from exc
                raise self._database_error(exc, reading=True) from exc
            finally:
                connection.set_progress_handler(None, 0)
        unique = {row[0]: row for row in candidates}
        if len(unique) > MAX_MEMORY_CANDIDATES:
            raise ProjectMemoryError(
                MemoryErrorCode.QUERY_LIMIT,
                "Memory candidate set exceeds the supported project search bound.",
            )
        return list(unique.values())

    @staticmethod
    def _index_record(connection: sqlite3.Connection, record: MemoryRecord) -> None:
        connection.execute(
            "DELETE FROM memory_terms WHERE project_id = ? AND memory_id = ?",
            (record.project_id, record.memory_id),
        )
        terms = {
            ("lexical", term)
            for term in _all_terms(f"{record.title} {record.content}")
        }
        terms.update(("path", path) for path in record.evidence_paths)
        terms.update(("filename", PurePosixPath(path).name) for path in record.evidence_paths)
        terms.update(("symbol", symbol) for symbol in record.evidence_symbols)
        connection.executemany(
            "INSERT INTO memory_terms(project_id, memory_id, kind, term) VALUES (?, ?, ?, ?)",
            (
                (record.project_id, record.memory_id, kind, term)
                for kind, term in sorted(terms)
            ),
        )

    def _decode(self, raw: str, project_id: str) -> MemoryRecord:
        try:
            record = MemoryRecord.from_dict(json.loads(raw), self.config)
        except (json.JSONDecodeError, TypeError, ValueError, ProjectMemoryError) as exc:
            raise ProjectMemoryError(
                MemoryErrorCode.STORE_CORRUPT,
                f"Stored project-memory record is invalid: {exc}",
            ) from exc
        if record.project_id != project_id:
            raise ProjectMemoryError(
                MemoryErrorCode.PROJECT_IDENTITY_MISMATCH,
                "Stored record project identity does not match the validated workspace.",
            )
        return record

    def _ensure_directory(self) -> None:
        try:
            self.application_storage.initialize()
            checked_path(self.directory)
            if not self.directory.exists():
                try:
                    self.directory.mkdir(mode=0o700)
                except FileExistsError:
                    checked_path(self.directory)
            info = self.directory.stat(follow_symlinks=False)
            if (
                not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) & 0o077
            ):
                raise ValueError("Project-memory directory must be private and user-owned.")
            if self.path.is_symlink():
                raise ValueError("Symlink project-memory databases are forbidden.")
            if self.path.exists():
                info = self.path.stat(follow_symlinks=False)
                if (
                    not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or stat.S_IMODE(info.st_mode) & 0o077
                ):
                    raise ValueError("Project-memory database must be a private, user-owned file.")
                if info.st_size > self.config.max_storage_bytes:
                    raise ProjectMemoryError(
                        MemoryErrorCode.CAPACITY_EXCEEDED,
                        "Existing project-memory database exceeds the configured byte limit.",
                    )
        except ProjectMemoryError:
            raise
        except (OSError, ValueError) as exc:
            raise ProjectMemoryError(MemoryErrorCode.STORE_UNAVAILABLE, str(exc)) from exc

    def _connect(self, *, create: bool, timeout_ms: int | None = None) -> sqlite3.Connection:
        self._ensure_directory()
        connection_timeout_ms = self.busy_timeout_ms if timeout_ms is None else timeout_ms
        with self._lock:
            if not self.path.exists():
                if not create:
                    raise ProjectMemoryError(
                        MemoryErrorCode.STORE_UNAVAILABLE,
                        "Project-memory database has not been initialized.",
                    )
                try:
                    descriptor = os.open(
                        self.path,
                        os.O_CREAT | os.O_EXCL | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0),
                        0o600,
                    )
                    os.close(descriptor)
                except FileExistsError:
                    pass
                except OSError as exc:
                    raise ProjectMemoryError(MemoryErrorCode.STORE_UNAVAILABLE, str(exc)) from exc
            connection: sqlite3.Connection | None = None
            try:
                connection = sqlite3.connect(
                    self.path,
                    timeout=connection_timeout_ms / 1000,
                    isolation_level=None,
                )
                connection.execute(f"PRAGMA busy_timeout = {connection_timeout_ms}")
                connection.execute("PRAGMA foreign_keys = ON")
                connection.execute("PRAGMA journal_mode = DELETE")
                connection.execute("PRAGMA synchronous = FULL")
                os.chmod(self.path, 0o600, follow_symlinks=False)
                return connection
            except sqlite3.DatabaseError as exc:
                if connection is not None:
                    connection.close()
                raise self._database_error(exc, reading=True) from exc
            except OSError as exc:
                if connection is not None:
                    connection.close()
                raise ProjectMemoryError(MemoryErrorCode.STORE_UNAVAILABLE, str(exc)) from exc

    @contextmanager
    def _connection(
        self, *, create: bool, reading: bool, timeout_ms: int | None = None,
    ) -> Generator[sqlite3.Connection, None, None]:
        connection = self._connect(create=create, timeout_ms=timeout_ms)
        try:
            yield connection
        except ProjectMemoryError:
            raise
        except sqlite3.DatabaseError as exc:
            raise self._database_error(exc, reading=reading) from exc
        finally:
            connection.close()

    def _ensure_schema(self, connection: sqlite3.Connection) -> None:
        try:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version > MEMORY_DB_SCHEMA_VERSION:
                raise ProjectMemoryError(
                    MemoryErrorCode.UNSUPPORTED_SCHEMA,
                    f"Project-memory schema {version} is newer than supported version {MEMORY_DB_SCHEMA_VERSION}.",
                )
            if version not in {0, 1, MEMORY_DB_SCHEMA_VERSION}:
                raise ProjectMemoryError(
                    MemoryErrorCode.UNSUPPORTED_SCHEMA,
                    f"Unsupported project-memory schema version {version}.",
                )
            if version == MEMORY_DB_SCHEMA_VERSION:
                return
            connection.execute("BEGIN IMMEDIATE")
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version == MEMORY_DB_SCHEMA_VERSION:
                connection.commit()
                return
            if version not in {0, 1}:
                raise ProjectMemoryError(
                    MemoryErrorCode.UNSUPPORTED_SCHEMA,
                    f"Unsupported project-memory schema version {version}.",
                )
            if version == 0:
                connection.execute(
                    "CREATE TABLE projects(project_id TEXT PRIMARY KEY, created_at TEXT NOT NULL)"
                )
                connection.execute(
                    "CREATE TABLE memories("
                    "memory_id TEXT PRIMARY KEY, project_id TEXT NOT NULL, capture_key TEXT, "
                    "status TEXT NOT NULL, updated_at TEXT NOT NULL, record_json TEXT NOT NULL, "
                    "UNIQUE(project_id, capture_key), "
                    "FOREIGN KEY(project_id) REFERENCES projects(project_id))"
                )
                connection.execute(
                    "CREATE INDEX memories_project_status_updated "
                    "ON memories(project_id, status, updated_at DESC, memory_id)"
                )
                connection.execute(
                    "CREATE TABLE tombstones("
                    "project_id TEXT NOT NULL, capture_key TEXT NOT NULL, "
                    "PRIMARY KEY(project_id, capture_key), "
                    "FOREIGN KEY(project_id) REFERENCES projects(project_id))"
                )
            connection.execute(
                "CREATE TABLE memory_terms("
                "project_id TEXT NOT NULL, memory_id TEXT NOT NULL, "
                "kind TEXT NOT NULL, term TEXT NOT NULL, "
                "PRIMARY KEY(project_id, memory_id, kind, term), "
                "FOREIGN KEY(memory_id) REFERENCES memories(memory_id) ON DELETE CASCADE)"
            )
            connection.execute(
                "CREATE INDEX memory_terms_project_kind_term "
                "ON memory_terms(project_id, kind, term, memory_id)"
            )
            if version == 1:
                rows = connection.execute(
                    "SELECT project_id, memory_id, record_json FROM memories "
                    "ORDER BY project_id, memory_id"
                ).fetchall()
                for project_id, _, raw in rows:
                    record = self._decode(raw, project_id)
                    self._index_record(connection, record)
                    self._check_storage_size(connection)
            connection.execute(f"PRAGMA user_version = {MEMORY_DB_SCHEMA_VERSION}")
            self._check_storage_size(connection)
            connection.commit()
        except ProjectMemoryError:
            connection.rollback()
            raise
        except sqlite3.DatabaseError as exc:
            connection.rollback()
            raise ProjectMemoryError(
                MemoryErrorCode.STORE_CORRUPT,
                f"Cannot initialize or migrate project-memory database: {exc}",
            ) from exc

    def _check_storage_size(self, connection: sqlite3.Connection) -> None:
        page_count = connection.execute("PRAGMA page_count").fetchone()[0]
        page_size = connection.execute("PRAGMA page_size").fetchone()[0]
        if page_count * page_size > self.config.max_storage_bytes:
            raise ProjectMemoryError(
                MemoryErrorCode.CAPACITY_EXCEEDED,
                "Project-memory index migration would exceed the configured storage limit.",
            )

    def _require_user_authorization(self, authorized: bool) -> None:
        if type(authorized) is not bool or not authorized:
            raise ProjectMemoryError(
                MemoryErrorCode.INVALID_RECORD,
                "Persistent manual memory changes require explicit user-originated authorization.",
            )

    def _check_retrieval(
        self, cancellation: threading.Event | None, deadline: float,
    ) -> None:
        if cancellation is not None and cancellation.is_set():
            raise ProjectMemoryError(MemoryErrorCode.CANCELLED, "Memory retrieval was cancelled.")
        if time.monotonic() > deadline:
            raise ProjectMemoryError(MemoryErrorCode.TIMEOUT, "Memory retrieval exceeded its time limit.")

    def _retrieval_timeout_ms(self, deadline: float) -> int:
        remaining = max(0.001, deadline - time.monotonic())
        return max(1, min(self.busy_timeout_ms, int(remaining * 1000)))

    @staticmethod
    def _database_error(exc: sqlite3.DatabaseError, *, reading: bool) -> ProjectMemoryError:
        message = str(exc)[:512]
        if "locked" in message.casefold() or "busy" in message.casefold():
            code = MemoryErrorCode.STORE_UNAVAILABLE
        elif "malformed" in message.casefold() or "not a database" in message.casefold():
            code = MemoryErrorCode.STORE_CORRUPT
        else:
            code = MemoryErrorCode.READ_FAILED if reading else MemoryErrorCode.WRITE_FAILED
        return ProjectMemoryError(code, f"Project-memory database operation failed: {message}")


def _relevance(
    record: MemoryRecord,
    terms: tuple[str, ...],
    relevant_paths: tuple[str, ...],
    relevant_symbols: tuple[str, ...],
) -> tuple[int, tuple[str, ...]]:
    score = 0
    reasons: list[str] = []
    matched_paths = set(record.evidence_paths) & set(relevant_paths)
    matched_filenames = {
        PurePosixPath(path).name for path in record.evidence_paths
    } & {
        PurePosixPath(path).name for path in relevant_paths
    }
    if matched_paths:
        score += 1200
        reasons.append("exact_evidence_path_match")
    elif matched_filenames:
        score += 1000
        reasons.append("evidence_filename_match")
    matched_symbols = set(relevant_symbols) & set(record.evidence_symbols)
    if matched_symbols:
        score += 900
        reasons.append("exact_symbol_match")
    matches = set(terms) & _token_set(f"{record.title} {record.content}")
    if matches:
        score += min(400, 100 * len(matches))
        reasons.append("task_keyword_match")
    if not (matched_paths or matched_filenames or matched_symbols or matches):
        return 0, ()
    if record.category == MemoryCategory.VERIFIED_OUTCOME:
        score += 40
        reasons.append("verified_task_provenance")
    if record.user_pinned:
        score += 20
        reasons.append("explicit_user_pinned_note")
    if record.evidence_paths and record.status == MemoryStatus.ACTIVE:
        score += 5
        reasons.append("active_source_linked_evidence")
    return score, tuple(reasons)


def _memory_context_length(record: MemoryRecord) -> int:
    return (
        len(record.title) + len(record.content) + len(record.category.value)
        + len(record.source_type.value) + sum(map(len, record.evidence_paths)) + 256
    )


def _timestamp_rank(record: MemoryRecord) -> float:
    return datetime.fromisoformat(record.updated_at).timestamp()


def _safe_relative_path(value: object) -> bool:
    if (
        not isinstance(value, str) or not value or len(value) > 512
        or "\\" in value or "\x00" in value or PurePosixPath(value).is_absolute()
        or ".." in PurePosixPath(value).parts
    ):
        return False
    return PurePosixPath(value).as_posix() == value and value not in {".", ""}


def _has_sensitive_content(value: str) -> bool:
    return any(pattern.search(value) for pattern in _SECRET_PATTERNS)


def _query_terms(value: str) -> tuple[str, ...]:
    terms = _all_terms(value)
    if len(terms) > 64:
        raise ProjectMemoryError(
            MemoryErrorCode.QUERY_LIMIT,
            "Memory queries are limited to 64 distinct normalized terms.",
        )
    return terms


def _all_terms(value: str) -> tuple[str, ...]:
    return tuple(dict.fromkeys(
        match.casefold()
        for match in _TERM.findall(value)
        if len(match) > 2 and match.casefold() not in _MEMORY_STOP_WORDS
    ))


def _token_set(value: str) -> set[str]:
    return set(_all_terms(value))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _valid_timestamp(value: str) -> bool:
    try:
        return datetime.fromisoformat(value).tzinfo is not None
    except (TypeError, ValueError):
        return False
