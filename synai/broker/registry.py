from __future__ import annotations

import hashlib
import hmac
import os
import sqlite3
import stat
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from synai.broker.config import BrokerConfig
from synai.broker.protocol import (
    BrokerError,
    LIFECYCLE_KINDS,
    TaskClaim,
    TOOL_KINDS,
    verify_claim,
)


TASK_STATES = frozenset({
    "preparing", "ready", "running", "cancelling", "completed", "failed",
    "interrupted", "uncertain", "cleaning_up", "removed",
})
_TRANSITIONS = {
    "preparing": {"ready", "failed", "cleaning_up", "interrupted", "uncertain"},
    "ready": {"running", "cleaning_up", "failed", "interrupted", "uncertain"},
    "running": {"cancelling", "completed", "failed", "interrupted", "uncertain"},
    "cancelling": {"failed", "interrupted", "uncertain", "cleaning_up"},
    "completed": {"cleaning_up"},
    "failed": {"cleaning_up"},
    "interrupted": {"cleaning_up", "uncertain"},
    "uncertain": {"cleaning_up"},
    "cleaning_up": {"removed", "uncertain"},
    "removed": set(),
}


class BrokerRegistry:
    """Private durable task, operation, request-replay and ownership ledger."""

    def __init__(self, config: BrokerConfig) -> None:
        self.config = config
        self.path = config.state_dir / "broker.sqlite3"

    def initialize(self) -> None:
        self.config.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = self.config.state_dir.lstat()
        if (
            not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode)
            or info.st_uid != os.geteuid() or info.st_mode & 0o077
        ):
            raise ValueError("Broker state directory must be private and owned by the Broker identity")
        with self.connect() as connection:
            connection.executescript("""
                PRAGMA journal_mode = WAL;
                CREATE TABLE IF NOT EXISTS broker_tasks (
                    task_id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    snapshot_id TEXT NOT NULL,
                    snapshot_digest TEXT NOT NULL,
                    target_id TEXT NOT NULL,
                    profile TEXT NOT NULL,
                    image_digest TEXT NOT NULL,
                    plan_digest TEXT NOT NULL,
                    policy_decision_id TEXT NOT NULL,
                    policy_version INTEGER NOT NULL,
                    approval_id TEXT NOT NULL,
                    lease_generation INTEGER NOT NULL,
                    fencing_token_hash TEXT NOT NULL,
                    state TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS broker_operations (
                    operation_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL REFERENCES broker_tasks(task_id),
                    kind TEXT NOT NULL,
                    state TEXT NOT NULL,
                    request_digest TEXT NOT NULL,
                    result_json TEXT,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS broker_containers (
                    task_id TEXT PRIMARY KEY REFERENCES broker_tasks(task_id),
                    allocation_id TEXT NOT NULL UNIQUE,
                    container_id TEXT UNIQUE,
                    image_digest TEXT NOT NULL,
                    snapshot_digest TEXT NOT NULL,
                    resource_profile TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    lifecycle_state TEXT NOT NULL,
                    termination_evidence TEXT,
                    cleanup_evidence TEXT
                );
                CREATE TABLE IF NOT EXISTS broker_request_nonces (
                    nonce TEXT PRIMARY KEY,
                    expires_at INTEGER NOT NULL
                );
            """)
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    "UPDATE broker_operations SET state = 'uncertain', updated_at = ? "
                    "WHERE state = 'claimed'",
                    (int(time.time()),),
                )
                connection.execute(
                    "UPDATE broker_tasks SET state = 'uncertain', updated_at = ? "
                    "WHERE state IN ('preparing', 'running', 'cancelling')",
                    (int(time.time()),),
                )
                connection.execute(
                    "UPDATE broker_tasks SET state = 'uncertain', updated_at = ? "
                    "WHERE state = 'ready' AND task_id IN "
                    "(SELECT task_id FROM broker_operations WHERE state = 'uncertain')",
                    (int(time.time()),),
                )
                connection.execute(
                    "UPDATE broker_containers SET lifecycle_state = 'quarantined' "
                    "WHERE lifecycle_state IN ('owned', 'allocating') AND task_id IN "
                    "(SELECT task_id FROM broker_tasks WHERE state = 'uncertain')",
                )
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        os.chmod(self.path, 0o600)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        flags = os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW
        descriptor = os.open(self.path, flags, 0o600)
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
                raise BrokerError("registry_unavailable", "Broker ownership registry is unsafe.", 503)
            connection = sqlite3.connect(self.path, timeout=2, isolation_level=None)
        except BaseException:
            os.close(descriptor)
            raise
        os.close(descriptor)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=2000")
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def consume_request_nonce(self, nonce: str, now: int) -> None:
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute("DELETE FROM broker_request_nonces WHERE expires_at <= ?", (now,))
                connection.execute(
                    "INSERT INTO broker_request_nonces(nonce, expires_at) VALUES (?, ?)",
                    (nonce, now + 60),
                )
                connection.commit()
            except sqlite3.IntegrityError as exc:
                connection.rollback()
                raise BrokerError("request_replayed", "Broker request nonce was already used.", 409) from exc
            except BaseException:
                connection.rollback()
                raise

    def authorize(self, claim: TaskClaim, signature: str, now: int | None = None) -> str:
        timestamp = int(time.time()) if now is None else now
        verify_claim(claim, signature, self.config.claim_key, timestamp)
        if (
            claim.image_digest != self.config.image_digest.removeprefix("sha256:")
            or claim.resource_profile != self.config.profile.name
            or claim.profile != self.config.profile.name
        ):
            raise BrokerError("claim_resource_mismatch", "Task claim requests an unapproved image or resource profile.", 403)
        if claim.tool_kind in TOOL_KINDS and claim.tool_kind not in self.config.allowed_tools:
            raise BrokerError("tool_not_allowed", "Task claim requests an unauthorized operation.", 403)
        request_digest = hashlib.sha256(
            f"{claim.task_id}\n{claim.operation_id}\n{claim.tool_kind}\n{claim.snapshot_digest}".encode(),
        ).hexdigest()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                replay = connection.execute(
                    "SELECT 1 FROM broker_operations WHERE operation_id = ?",
                    (claim.operation_id,),
                ).fetchone()
                if replay is not None:
                    raise BrokerError("operation_replayed", "Task operation identity was already used.", 409)
                task = connection.execute(
                    "SELECT * FROM broker_tasks WHERE task_id = ?", (claim.task_id,),
                ).fetchone()
                if claim.tool_kind == "prepare":
                    if task is not None:
                        raise BrokerError("task_exists", "Task identity is already registered.", 409)
                    connection.execute(
                        "INSERT INTO broker_tasks(task_id, project_id, snapshot_id, snapshot_digest, "
                        "target_id, profile, image_digest, plan_digest, policy_decision_id, policy_version, "
                        "approval_id, lease_generation, fencing_token_hash, state, created_at, updated_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'preparing', ?, ?)",
                        (
                            claim.task_id, claim.project_id, claim.snapshot_id, claim.snapshot_digest,
                            claim.target_id, claim.profile, claim.image_digest, claim.plan_digest,
                            claim.policy_decision_id, claim.policy_version, claim.approval_id,
                            claim.lease_generation,
                            hashlib.sha256(claim.fencing_token.encode()).hexdigest(), timestamp, timestamp,
                        ),
                    )
                else:
                    self._validate_task(task, claim)
                connection.execute(
                    "INSERT INTO broker_operations(operation_id, task_id, kind, state, request_digest, "
                    "created_at, updated_at) VALUES (?, ?, ?, 'claimed', ?, ?, ?)",
                    (claim.operation_id, claim.task_id, claim.tool_kind, request_digest, timestamp, timestamp),
                )
                connection.commit()
            except sqlite3.IntegrityError as exc:
                connection.rollback()
                raise BrokerError("operation_replayed", "Task operation identity was already used.", 409) from exc
            except BaseException:
                connection.rollback()
                raise
        return request_digest

    @staticmethod
    def _validate_task(task: sqlite3.Row | None, claim: TaskClaim) -> None:
        if task is None:
            raise BrokerError("task_not_registered", "Task identity is not registered.", 404)
        if any(task[name] != value for name, value in (
            ("project_id", claim.project_id), ("snapshot_id", claim.snapshot_id),
            ("snapshot_digest", claim.snapshot_digest), ("target_id", claim.target_id),
            ("profile", claim.profile), ("image_digest", claim.image_digest),
            ("plan_digest", claim.plan_digest), ("policy_decision_id", claim.policy_decision_id),
            ("policy_version", claim.policy_version), ("approval_id", claim.approval_id),
        )):
            raise BrokerError("task_binding_mismatch", "Task claim does not match its registered immutable identity.", 403)
        if claim.lease_generation < task["lease_generation"]:
            raise BrokerError("stale_fence", "Task claim lease generation is stale.", 409)
        if claim.lease_generation > task["lease_generation"]:
            raise BrokerError("fence_update_required", "A new fencing generation requires an explicit registered transfer.", 409)
        actual = hashlib.sha256(claim.fencing_token.encode()).hexdigest()
        if not hmac.compare_digest(actual, task["fencing_token_hash"]):
            raise BrokerError("stale_fence", "Task fencing identity is invalid.", 409)

    def transition(self, task_id: str, state: str, now: int | None = None) -> None:
        timestamp = int(time.time()) if now is None else now
        if state not in TASK_STATES:
            raise ValueError("Unknown Broker task state")
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute("SELECT state FROM broker_tasks WHERE task_id = ?", (task_id,)).fetchone()
                if row is None:
                    raise BrokerError("task_not_found", "Task identity was not found.", 404)
                if state not in _TRANSITIONS[row["state"]]:
                    raise BrokerError("task_transition_invalid", "Broker task state transition is invalid.", 409)
                connection.execute(
                    "UPDATE broker_tasks SET state = ?, updated_at = ? WHERE task_id = ?",
                    (state, timestamp, task_id),
                )
                connection.commit()
            except BaseException:
                connection.rollback()
                raise

    def reserve_container(self, task_id: str, allocation_id: str, now: int | None = None) -> None:
        timestamp = int(time.time()) if now is None else now
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM broker_tasks WHERE task_id = ?", (task_id,)).fetchone()
            if row is None or row["state"] != "ready":
                raise BrokerError("task_state_invalid", "Task is not ready to start.", 409)
            connection.execute(
                "INSERT INTO broker_containers(task_id, allocation_id, container_id, image_digest, "
                "snapshot_digest, resource_profile, created_at, lifecycle_state) "
                "VALUES (?, ?, NULL, ?, ?, ?, ?, 'allocating')",
                (
                    task_id, allocation_id, row["image_digest"],
                    row["snapshot_digest"], row["profile"], timestamp,
                ),
            )

    def bind_container(self, task_id: str, allocation_id: str, container_id: str) -> None:
        if not container_id or len(container_id) > 128 or any(ch not in "0123456789abcdef" for ch in container_id):
            raise BrokerError("runtime_response_invalid", "Podman returned an invalid container identity.", 502)
        with self.connect() as connection:
            cursor = connection.execute(
                "UPDATE broker_containers SET container_id = ?, lifecycle_state = 'owned' "
                "WHERE task_id = ? AND allocation_id = ? AND lifecycle_state = 'allocating' "
                "AND container_id IS NULL",
                (container_id, task_id, allocation_id),
            )
            if cursor.rowcount != 1:
                raise BrokerError("container_ownership_invalid", "Container allocation is not registered.", 409)

    def owned_container(self, task_id: str) -> sqlite3.Row:
        row = self.container_record(task_id)
        if row is None or row["lifecycle_state"] not in {"owned", "terminated"} or row["container_id"] is None:
            raise BrokerError("container_quarantined", "Task container ownership is not currently verified.", 409)
        return row

    def container_record(self, task_id: str) -> sqlite3.Row | None:
        with self.connect() as connection:
            return connection.execute(
                "SELECT c.*, t.project_id, t.snapshot_id, t.state AS task_state FROM broker_containers c "
                "JOIN broker_tasks t ON t.task_id = c.task_id WHERE c.task_id = ?",
                (task_id,),
            ).fetchone()

    def complete_operation(self, operation_id: str, state: str, result_json: str | None = None) -> None:
        if state not in {"completed", "failed", "uncertain"}:
            raise ValueError("Invalid Broker operation terminal state")
        with self.connect() as connection:
            cursor = connection.execute(
                "UPDATE broker_operations SET state = ?, result_json = ?, updated_at = ? "
                "WHERE operation_id = ? AND state = 'claimed'",
                (state, result_json, int(time.time()), operation_id),
            )
            if cursor.rowcount != 1:
                raise BrokerError("operation_state_invalid", "Operation is not in a claimable state.", 409)

    def task(self, task_id: str) -> sqlite3.Row:
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM broker_tasks WHERE task_id = ?", (task_id,)).fetchone()
        if row is None:
            raise BrokerError("task_not_found", "Task identity was not found.", 404)
        return row

    def mark_container(self, task_id: str, state: str, *, termination: str | None = None,
                       cleanup: str | None = None) -> None:
        if state not in {"owned", "quarantined", "terminated", "removed"}:
            raise ValueError("Invalid container ownership state")
        with self.connect() as connection:
            cursor = connection.execute(
                "UPDATE broker_containers SET lifecycle_state = ?, "
                "termination_evidence = coalesce(?, termination_evidence), "
                "cleanup_evidence = coalesce(?, cleanup_evidence) WHERE task_id = ?",
                (state, termination, cleanup, task_id),
            )
            if cursor.rowcount != 1:
                raise BrokerError("container_not_owned", "No private container ownership record exists.", 404)
