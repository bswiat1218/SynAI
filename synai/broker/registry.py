from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import stat
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from enum import StrEnum
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


class RegistryTrust(StrEnum):
    STORAGE_UNVERIFIED = "state_storage_unverified"
    NOT_OPENED = "storage_verified_registry_not_opened"
    RECOVERABLE = "registry_verified_recoverable"
    UNAVAILABLE = "registry_unavailable"
    CORRUPT = "registry_corrupt"
    MISMATCH = "registry_identity_mismatch"
    RECOVERY_INCOMPLETE = "recovery_incomplete"
    COMPLETE = "recovery_verified_complete"


class BrokerRegistry:
    """Private durable task, operation, request-replay and ownership ledger."""

    def __init__(self, config: BrokerConfig) -> None:
        self.config = config
        self.path = config.state_dir / "broker.sqlite3"
        self.seal_path = config.state_dir / "registry-identity.json"
        self.dirty_path = config.state_dir / "registry-interrupted"
        self.trust = RegistryTrust.STORAGE_UNVERIFIED
        self.failure_code: str | None = None
        self.failure_callback: Callable[[str], None] | None = None
        self.storage_check: Callable[[], bool] | None = None
        self._lock = threading.RLock()
        self._initializing_thread: int | None = None

    @property
    def _initializing(self) -> bool:
        return self._initializing_thread == threading.get_ident()

    def fail(self, state: RegistryTrust, code: str) -> None:
        self.trust = state
        self.failure_code = code
        if self.failure_callback is not None:
            self.failure_callback(code)

    def require_execution_authority(self) -> None:
        if self.failure_code is not None or self.trust not in {
            RegistryTrust.RECOVERABLE, RegistryTrust.COMPLETE,
        }:
            raise BrokerError("registry_unavailable", "Registry authority does not permit new work or slot reuse.", 503)

    def status(self) -> dict[str, object]:
        authority = self.failure_code in {None, "recovery_incomplete"} and self.trust in {
            RegistryTrust.RECOVERABLE, RegistryTrust.RECOVERY_INCOMPLETE,
            RegistryTrust.COMPLETE,
        }
        return {
            "state": self.trust.value,
            "error_code": self.failure_code,
            "operator_intervention_required": self.failure_code is not None,
            "ownership_authority_available": authority,
            "container_identity_unavailable": not authority,
            "containment_unresolved": self.failure_code is not None,
        }

    @contextmanager
    def _serialized(self) -> Iterator[None]:
        if not self._lock.acquire(timeout=2):
            self.fail(RegistryTrust.UNAVAILABLE, "registry_busy")
            raise BrokerError("registry_busy", "Registry access did not complete within its bound.", 503)
        try:
            yield
        finally:
            self._lock.release()

    def _policy_digest(self) -> str:
        from synai.broker.storage_inspector import inventory_generation
        policy = {
            "inventory": inventory_generation(self.config.storage_roles),
            "slots": [repr(slot) for slot in self.config.quota_slots],
            "uid": self.config.service_uid, "gid": self.config.service_gid,
            "claim_key": self.config.claim_key.hex(),
            "image": self.config.image, "profile": repr(self.config.profile),
        }
        return hashlib.sha256(json.dumps(policy, sort_keys=True).encode()).hexdigest()

    def _identity(self) -> dict[str, object]:
        info = self.path.lstat()
        if (
            not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
            or info.st_mode & 0o077
            or info.st_size > 512 * 1024 * 1024
        ):
            raise BrokerError("registry_identity_mismatch", "Registry identity is unsafe.", 503)
        digest = hashlib.sha256()
        with self.path.open("rb") as source:
            for chunk in iter(lambda: source.read(65536), b""):
                digest.update(chunk)
        return {
            "schema_version": 1, "device": info.st_dev, "inode": info.st_ino,
            "sha256": digest.hexdigest(), "policy": self._policy_digest(),
        }

    def _sync_directory(self) -> None:
        descriptor = os.open(self.config.state_dir, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _seal(self) -> None:
        temporary = self.seal_path.with_suffix(".pending")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "w") as target:
            json.dump(self._identity(), target, sort_keys=True)
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary, self.seal_path)
        self._sync_directory()

    def _verify_seal(self) -> None:
        if self.dirty_path.exists() or self.seal_path.with_suffix(".pending").exists():
            raise BrokerError("registry_recovery_evidence_missing", "Registry access was interrupted; operator recovery is required.", 503)
        for suffix in ("-wal", "-shm", "-journal"):
            if self.path.with_name(self.path.name + suffix).exists():
                raise BrokerError("registry_journal_unexpected", "Registry journal evidence requires operator recovery.", 503)
        info = self.seal_path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077 or info.st_size > 4096:
            raise BrokerError("registry_identity_mismatch", "Registry seal is unsafe.", 503)
        try:
            seal = json.loads(self.seal_path.read_text())
        except (ValueError, UnicodeError) as exc:
            raise BrokerError("registry_identity_mismatch", "Registry seal is invalid.", 503) from exc
        if seal != self._identity():
            raise BrokerError("registry_identity_mismatch", "Registry or storage policy changed; operator recovery is required.", 503)

    def initialize(self, *, storage_verified: bool = False, provision: bool = False,
                   adopt_legacy: bool = False) -> None:
        """Provisioning/adoption are explicit offline operator actions, never service startup."""
        if not storage_verified:
            self.fail(RegistryTrust.STORAGE_UNVERIFIED, "state_storage_unverified")
            raise BrokerError("state_storage_unverified", "Bounded registry storage is not verified.", 503)
        self.trust = RegistryTrust.NOT_OPENED
        try:
            with self._serialized():
                if not self.path.exists():
                    if not provision or self.seal_path.exists() or self.dirty_path.exists():
                        raise BrokerError("registry_unavailable", "Historical registry is missing; no replacement will be created.", 503)
                    descriptor = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                    os.close(descriptor)
                elif not self.seal_path.exists() and not adopt_legacy:
                    raise BrokerError("registry_recovery_evidence_missing", "Registry identity evidence is missing; operator adoption is required.", 503)
                if self.seal_path.exists():
                    self._verify_seal()
                elif self.dirty_path.exists():
                    raise BrokerError("registry_recovery_evidence_missing", "Interrupted provisioning requires operator recovery.", 503)
                self._initializing_thread = threading.get_ident()
                try:
                    self._initialize_schema()
                    self.verify_ownership()
                finally:
                    self._initializing_thread = None
                self.trust = RegistryTrust.RECOVERABLE
                self.failure_code = None
        except (BrokerError, OSError, sqlite3.Error) as exc:
            self._access_failed(exc)
            raise

    def _initialize_schema(self) -> None:
        info = self.config.state_dir.lstat()
        if (
            not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode)
            or info.st_uid != os.geteuid() or info.st_mode & 0o077
        ):
            raise ValueError("Broker state directory must be private and owned by the Broker identity")
        with self.connect() as connection:
            if self.seal_path.exists():
                expected = {
                    "broker_tasks", "broker_operations", "broker_containers",
                    "broker_request_nonces", "broker_slots", "broker_storage_roles",
                }
                tables = {row[0] for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'",
                )}
                if not expected.issubset(tables):
                    raise BrokerError("registry_corrupt", "Historical registry schema is incomplete.", 503)
                self._verify_ownership(connection)
            connection.executescript("""
                PRAGMA journal_mode = WAL;
                CREATE TABLE IF NOT EXISTS broker_tasks (
                    task_id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    snapshot_id TEXT NOT NULL,
                    snapshot_digest TEXT NOT NULL,
                    allocation_id TEXT NOT NULL,
                    target_id TEXT NOT NULL,
                    profile TEXT NOT NULL,
                    image_digest TEXT NOT NULL,
                    plan_digest TEXT NOT NULL,
                    policy_decision_id TEXT NOT NULL,
                    policy_version INTEGER NOT NULL,
                    approval_id TEXT NOT NULL,
                    slot_id TEXT,
                    slot_owner_token_hash TEXT,
                    lease_generation INTEGER NOT NULL,
                    fencing_token_hash TEXT NOT NULL,
                    state TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS broker_operations (
                    operation_id TEXT PRIMARY KEY,
                    claim_id TEXT NOT NULL UNIQUE,
                    replay_nonce TEXT NOT NULL UNIQUE,
                    request_id TEXT NOT NULL UNIQUE,
                    task_id TEXT NOT NULL REFERENCES broker_tasks(task_id),
                    kind TEXT NOT NULL,
                    state TEXT NOT NULL,
                    request_digest TEXT NOT NULL,
                    claim_json TEXT,
                    claim_signature TEXT,
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
                    cleanup_evidence TEXT,
                    reconciliation_evidence TEXT
                );
                CREATE TABLE IF NOT EXISTS broker_request_nonces (
                    nonce TEXT PRIMARY KEY,
                    expires_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS broker_slots (
                    slot_id TEXT PRIMARY KEY,
                    path TEXT NOT NULL,
                    filesystem_uuid TEXT NOT NULL,
                    project_id INTEGER NOT NULL,
                    byte_limit INTEGER NOT NULL,
                    inode_limit INTEGER NOT NULL,
                    state TEXT NOT NULL CHECK (state IN ('free', 'allocated', 'release_pending', 'quarantined')),
                    task_id TEXT UNIQUE,
                    allocation_id TEXT UNIQUE,
                    owner_token_hash TEXT,
                    release_evidence TEXT,
                    updated_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS broker_storage_roles (
                    role_id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    path TEXT NOT NULL,
                    mount_path TEXT NOT NULL,
                    filesystem_uuid TEXT NOT NULL,
                    project_id INTEGER NOT NULL,
                    byte_limit INTEGER NOT NULL,
                    inode_limit INTEGER NOT NULL,
                    owner_uid INTEGER NOT NULL,
                    owner_gid INTEGER NOT NULL,
                    policy_generation TEXT NOT NULL,
                    updated_at INTEGER NOT NULL
                );
            """)
            columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(broker_operations)")
            }
            for column in (
                "claim_id", "replay_nonce", "request_id", "claim_json", "claim_signature",
            ):
                if column not in columns:
                    connection.execute(f"ALTER TABLE broker_operations ADD COLUMN {column} TEXT")
            container_columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(broker_containers)")
            }
            if "reconciliation_evidence" not in container_columns:
                connection.execute("ALTER TABLE broker_containers ADD COLUMN reconciliation_evidence TEXT")
            task_columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(broker_tasks)")
            }
            if "allocation_id" not in task_columns:
                connection.execute("ALTER TABLE broker_tasks ADD COLUMN allocation_id TEXT")
            if "slot_id" not in task_columns:
                connection.execute("ALTER TABLE broker_tasks ADD COLUMN slot_id TEXT")
            if "slot_owner_token_hash" not in task_columns:
                connection.execute("ALTER TABLE broker_tasks ADD COLUMN slot_owner_token_hash TEXT")
            for slot in self.config.quota_slots:
                connection.execute(
                    "INSERT OR IGNORE INTO broker_slots(slot_id, path, filesystem_uuid, project_id, "
                    "byte_limit, inode_limit, state, updated_at) VALUES (?, ?, ?, ?, ?, ?, 'free', ?)",
                    (
                        slot.slot_id, str(slot.path), slot.filesystem_uuid, slot.project_id,
                        slot.byte_limit, slot.inode_limit, int(time.time()),
                    ),
                )
            from synai.broker.storage_inspector import inventory_generation
            generation = inventory_generation(self.config.storage_roles)
            for role in self.config.storage_roles:
                connection.execute(
                    "INSERT OR IGNORE INTO broker_storage_roles "
                    "(role_id, kind, path, mount_path, filesystem_uuid, project_id, "
                    "byte_limit, inode_limit, owner_uid, owner_gid, policy_generation, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        role.role_id, role.kind, str(role.path), str(role.mount_path),
                        role.filesystem_uuid, role.project_id, role.byte_limit,
                        role.inode_limit, role.owner_uid, role.owner_gid,
                        generation, int(time.time()),
                    ),
                )
            connection.executescript("""
                CREATE UNIQUE INDEX IF NOT EXISTS broker_operation_claim_id
                    ON broker_operations(claim_id) WHERE claim_id IS NOT NULL;
                CREATE UNIQUE INDEX IF NOT EXISTS broker_operation_replay_nonce
                    ON broker_operations(replay_nonce) WHERE replay_nonce IS NOT NULL;
                CREATE UNIQUE INDEX IF NOT EXISTS broker_operation_request_id
                    ON broker_operations(request_id) WHERE request_id IS NOT NULL;
                CREATE UNIQUE INDEX IF NOT EXISTS broker_slot_owner_token
                    ON broker_slots(owner_token_hash) WHERE owner_token_hash IS NOT NULL;
            """)
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    "UPDATE broker_operations SET state = 'uncertain', updated_at = ? "
                        "WHERE state IN ('claimed', 'running')",
                    (int(time.time()),),
                )
                connection.execute(
                    "UPDATE broker_tasks SET state = 'uncertain', updated_at = ? "
                    "WHERE state IN ('preparing', 'running', 'cancelling', 'cleaning_up')",
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
                connection.execute(
                    "UPDATE broker_slots SET state = 'quarantined', updated_at = ? "
                    "WHERE state IN ('allocated', 'release_pending') AND task_id IN "
                    "(SELECT task_id FROM broker_tasks WHERE state = 'uncertain')",
                    (int(time.time()),),
                )
                connection.execute(
                    "UPDATE broker_slots SET state = 'quarantined', updated_at = ? "
                    "WHERE state = 'release_pending' AND task_id IN "
                    "(SELECT task_id FROM broker_operations WHERE state = 'uncertain')",
                    (int(time.time()),),
                )
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        os.chmod(self.path, 0o600)

    def verify_ownership(self) -> None:
        with self.connect() as connection:
            self._verify_ownership(connection)

    @staticmethod
    def _verify_ownership(connection: sqlite3.Connection) -> None:
        if connection.execute("""
                SELECT 1 FROM broker_containers c
                LEFT JOIN broker_tasks t ON t.task_id = c.task_id
                WHERE t.task_id IS NULL OR c.allocation_id IS NOT t.allocation_id
                   OR c.image_digest IS NOT t.image_digest
                   OR c.snapshot_digest IS NOT t.snapshot_digest
                   OR c.resource_profile IS NOT t.profile LIMIT 1
            """).fetchone() is not None:
            raise BrokerError("registry_identity_mismatch", "Container ownership history is inconsistent.", 503)
        if connection.execute("""
                SELECT 1 FROM broker_slots s LEFT JOIN broker_tasks t ON t.task_id = s.task_id
                WHERE (s.state = 'free' AND (s.task_id IS NOT NULL OR s.allocation_id IS NOT NULL))
                   OR (s.state != 'free' AND (
                       t.task_id IS NULL OR t.slot_id IS NOT s.slot_id
                       OR t.allocation_id IS NOT s.allocation_id
                       OR t.slot_owner_token_hash IS NOT s.owner_token_hash)) LIMIT 1
            """).fetchone() is not None:
            raise BrokerError("registry_identity_mismatch", "Quota-slot ownership history is inconsistent.", 503)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        if not self._initializing and self.storage_check is not None and not self.storage_check():
            self.fail(RegistryTrust.STORAGE_UNVERIFIED, "state_storage_unverified")
            raise BrokerError("state_storage_unverified", "Bounded registry storage is unavailable.", 503)
        with self._serialized():
            connection = None
            try:
                if self.failure_code not in {None, "recovery_incomplete"} and not self._initializing:
                    raise BrokerError(self.failure_code, "Registry trust is revoked; restart and operator recovery are required.", 503)
                if not self._initializing:
                    self._verify_seal()
                descriptor = os.open(self.path, os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW)
                try:
                    info = os.fstat(descriptor)
                    if (
                        not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                        or info.st_mode & 0o077 or info.st_size > 512 * 1024 * 1024
                    ):
                        raise BrokerError("registry_identity_mismatch", "Registry ownership or type is unsafe.", 503)
                finally:
                    os.close(descriptor)
                marker = os.open(self.dirty_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                try:
                    os.fsync(marker)
                finally:
                    os.close(marker)
                self._sync_directory()
                connection = sqlite3.connect(self.path.as_uri() + "?mode=rw", uri=True, timeout=2, isolation_level=None)
                connection.row_factory = sqlite3.Row
                connection.execute("PRAGMA foreign_keys=ON")
                connection.execute("PRAGMA busy_timeout=2000")
                connection.execute("PRAGMA synchronous=FULL")
                if [tuple(row) for row in connection.execute("PRAGMA integrity_check")] != [("ok",)]:
                    raise BrokerError("registry_corrupt", "Registry integrity is inconclusive.", 503)
                if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
                    raise BrokerError("registry_corrupt", "Registry references are inconsistent.", 503)
                rejection = None
                try:
                    with connection:
                        yield connection
                except BrokerError as exc:
                    if exc.status_code == 503:
                        raise
                    if connection.in_transaction:
                        connection.rollback()
                    rejection = exc
                finally:
                    if connection.in_transaction:
                        connection.rollback()
                if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
                    raise BrokerError("registry_corrupt", "Registry references are inconsistent.", 503)
                if connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal":
                    if any(not self.path.with_name(self.path.name + suffix).is_file()
                           for suffix in ("-wal", "-shm")):
                        raise BrokerError("registry_journal_unexpected", "Active registry journal evidence is missing.", 503)
                checkpoint = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
                if checkpoint is not None and checkpoint[0] != 0:
                    raise BrokerError("registry_checkpoint_failed", "Registry checkpoint did not complete.", 503)
                connection.close()
                connection = None
                self._seal()
                self.dirty_path.unlink()
                self._sync_directory()
                if rejection is not None:
                    raise rejection
            except BrokerError as exc:
                if exc.status_code == 503:
                    self._access_failed(exc)
                raise
            except (OSError, sqlite3.Error) as exc:
                self._access_failed(exc)
                raise BrokerError(
                    self.failure_code or "registry_unavailable",
                    "Registry access or durability failed; operator recovery is required.",
                    503,
                ) from exc
            except BaseException:
                self.fail(RegistryTrust.RECOVERY_INCOMPLETE, "registry_access_interrupted")
                raise
            finally:
                if connection is not None:
                    try:
                        connection.close()
                    except sqlite3.Error as exc:
                        self._access_failed(exc)
                        raise

    def _access_failed(self, exc: Exception) -> None:
        code = exc.code if isinstance(exc, BrokerError) else "registry_unavailable"
        if isinstance(exc, sqlite3.DatabaseError) and not isinstance(exc, sqlite3.OperationalError):
            code = "registry_corrupt"
        state = {
            "state_storage_unverified": RegistryTrust.STORAGE_UNVERIFIED,
            "registry_corrupt": RegistryTrust.CORRUPT,
            "registry_identity_mismatch": RegistryTrust.MISMATCH,
            "registry_recovery_evidence_missing": RegistryTrust.RECOVERY_INCOMPLETE,
            "registry_journal_unexpected": RegistryTrust.RECOVERY_INCOMPLETE,
        }.get(code, RegistryTrust.UNAVAILABLE)
        self.fail(state, code)

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

    def authorize(
        self,
        claim: TaskClaim,
        signature: str,
        now: int | None = None,
        *,
        eligible_slot_ids: frozenset[str] | None = None,
    ) -> str:
        self.require_execution_authority()
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
                    "SELECT 1 FROM broker_operations WHERE operation_id = ? OR claim_id = ? "
                    "OR replay_nonce = ? OR request_id = ?",
                    (claim.operation_id, claim.claim_id, claim.replay_nonce, claim.request_id),
                ).fetchone()
                if replay is not None:
                    raise BrokerError("operation_replayed", "Task operation identity was already used.", 409)
                task = connection.execute(
                    "SELECT * FROM broker_tasks WHERE task_id = ?", (claim.task_id,),
                ).fetchone()
                if claim.tool_kind == "prepare":
                    if task is not None:
                        raise BrokerError("task_exists", "Task identity is already registered.", 409)
                    configured_ids = tuple(
                        slot.slot_id for slot in self.config.quota_slots
                        if eligible_slot_ids is None or slot.slot_id in eligible_slot_ids
                    )
                    if not configured_ids:
                        raise BrokerError(
                            "workspace_quota_unverified",
                            "No pre-provisioned Broker quota slots are configured.",
                            503,
                        )
                    marks = ",".join("?" for _ in configured_ids)
                    slot = connection.execute(
                        f"SELECT * FROM broker_slots WHERE state = 'free' "
                        f"AND slot_id IN ({marks}) ORDER BY slot_id LIMIT 1",
                        configured_ids,
                    ).fetchone()
                    if slot is None:
                        raise BrokerError(
                            "quota_slot_pool_exhausted",
                            "No verified Broker quota slot is available.",
                            409,
                        )
                    configured = next(item for item in self.config.quota_slots if item.slot_id == slot["slot_id"])
                    if any(slot[column] != value for column, value in (
                        ("path", str(configured.path)),
                        ("filesystem_uuid", configured.filesystem_uuid),
                        ("project_id", configured.project_id),
                        ("byte_limit", configured.byte_limit),
                        ("inode_limit", configured.inode_limit),
                    )):
                        raise BrokerError(
                            "workspace_slot_mismatch",
                            "Durable quota slot inventory differs from operator configuration.",
                            503,
                        )
                    owner_token_hash = hashlib.sha256(secrets.token_bytes(32)).hexdigest()
                    claimed = connection.execute(
                        "UPDATE broker_slots SET state = 'allocated', task_id = ?, allocation_id = ?, "
                        "owner_token_hash = ?, release_evidence = NULL, updated_at = ? "
                        "WHERE slot_id = ? AND state = 'free'",
                        (
                            claim.task_id, claim.allocation_id, owner_token_hash,
                            timestamp, slot["slot_id"],
                        ),
                    )
                    if claimed.rowcount != 1:
                        raise BrokerError(
                            "quota_slot_pool_exhausted",
                            "Quota slot ownership changed during allocation.",
                            409,
                        )
                    connection.execute(
                        "INSERT INTO broker_tasks(task_id, project_id, snapshot_id, snapshot_digest, allocation_id, "
                        "target_id, profile, image_digest, plan_digest, policy_decision_id, policy_version, "
                        "approval_id, slot_id, slot_owner_token_hash, lease_generation, fencing_token_hash, "
                        "state, created_at, updated_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'preparing', ?, ?)",
                        (
                            claim.task_id, claim.project_id, claim.snapshot_id, claim.snapshot_digest,
                            claim.allocation_id,
                            claim.target_id, claim.profile, claim.image_digest, claim.plan_digest,
                            claim.policy_decision_id, claim.policy_version, claim.approval_id,
                            slot["slot_id"], owner_token_hash,
                            claim.lease_generation,
                            hashlib.sha256(claim.fencing_token.encode()).hexdigest(), timestamp, timestamp,
                        ),
                    )
                else:
                    self._validate_task(task, claim)
                connection.execute(
                    "INSERT INTO broker_operations(operation_id, claim_id, replay_nonce, request_id, "
                    "task_id, kind, state, request_digest, claim_json, claim_signature, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, 'claimed', ?, ?, ?, ?, ?)",
                    (
                        claim.operation_id, claim.claim_id, claim.replay_nonce,
                        claim.request_id, claim.task_id, claim.tool_kind,
                        request_digest, json.dumps(claim.to_dict(), sort_keys=True, separators=(",", ":")),
                        signature, timestamp, timestamp,
                    ),
                )
                connection.commit()
            except sqlite3.IntegrityError as exc:
                connection.rollback()
                raise BrokerError("operation_replayed", "Task operation identity was already used.", 409) from exc
            except BaseException:
                connection.rollback()
                raise
        return request_digest

    def begin_operation(self, operation_id: str) -> None:
        self.require_execution_authority()
        with self.connect() as connection:
            cursor = connection.execute(
                "UPDATE broker_operations SET state = 'running', updated_at = ? "
                "WHERE operation_id = ? AND state = 'claimed'",
                (int(time.time()), operation_id),
            )
            if cursor.rowcount != 1:
                raise BrokerError("operation_state_invalid", "Operation is not claimable for dispatch.", 409)

    @staticmethod
    def _validate_task(task: sqlite3.Row | None, claim: TaskClaim) -> None:
        if task is None:
            raise BrokerError("task_not_registered", "Task identity is not registered.", 404)
        if any(task[name] != value for name, value in (
            ("project_id", claim.project_id), ("snapshot_id", claim.snapshot_id),
            ("snapshot_digest", claim.snapshot_digest), ("target_id", claim.target_id),
            ("allocation_id", claim.allocation_id),
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
        self.require_execution_authority()
        timestamp = int(time.time()) if now is None else now
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM broker_tasks WHERE task_id = ?", (task_id,)).fetchone()
            if (
                row is None or row["state"] != "ready"
                or row["allocation_id"] != allocation_id
            ):
                connection.rollback()
                raise BrokerError("task_state_invalid", "Task is not ready to start.", 409)
            if not row["slot_id"] or row["slot_owner_token_hash"] is None:
                connection.rollback()
                raise BrokerError(
                    "workspace_slot_missing",
                    "Task has no durable quota-slot assignment.",
                    409,
                )
            slot = connection.execute(
                "SELECT state, task_id, allocation_id, owner_token_hash FROM broker_slots WHERE slot_id = ?",
                (row["slot_id"],),
            ).fetchone()
            if (
                slot is None or slot["state"] != "allocated"
                or slot["task_id"] != task_id or slot["allocation_id"] != allocation_id
                or not hmac.compare_digest(slot["owner_token_hash"] or "", row["slot_owner_token_hash"])
            ):
                connection.rollback()
                raise BrokerError("workspace_slot_mismatch", "Task quota-slot ownership is not verified.", 409)
            active = connection.execute(
                "SELECT count(*) FROM broker_containers c JOIN broker_tasks t USING(task_id) "
                "WHERE c.lifecycle_state != 'removed' AND t.state != 'removed'",
            ).fetchone()[0]
            if active >= self.config.max_active_tasks:
                connection.rollback()
                raise BrokerError(
                    "active_task_limit",
                    "Broker active task limit has been reached.",
                    409,
                )
            try:
                connection.execute(
                    "INSERT INTO broker_containers(task_id, allocation_id, container_id, image_digest, "
                    "snapshot_digest, resource_profile, created_at, lifecycle_state) "
                    "VALUES (?, ?, NULL, ?, ?, ?, ?, 'allocating')",
                    (
                        task_id, allocation_id, row["image_digest"],
                        row["snapshot_digest"], row["profile"], timestamp,
                    ),
                )
                connection.commit()
            except BaseException:
                connection.rollback()
                raise

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
                "SELECT c.*, t.project_id, t.snapshot_id, t.state AS task_state, "
                "t.allocation_id AS task_allocation_id, t.image_digest AS task_image_digest, "
                "t.snapshot_digest AS task_snapshot_digest, t.profile AS task_profile "
                "FROM broker_containers c "
                "JOIN broker_tasks t ON t.task_id = c.task_id WHERE c.task_id = ?",
                (task_id,),
            ).fetchone()

    def container_records(self) -> tuple[sqlite3.Row, ...]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT c.*, t.project_id, t.snapshot_id, t.state AS task_state, "
                "t.allocation_id AS task_allocation_id, t.image_digest AS task_image_digest, "
                "t.snapshot_digest AS task_snapshot_digest, t.profile AS task_profile "
                "FROM broker_containers c JOIN broker_tasks t USING(task_id) "
                "ORDER BY c.created_at, c.task_id",
            ).fetchall()
        return tuple(rows)

    def record_reconciliation(
        self,
        task_id: str,
        lifecycle_state: str,
        evidence: str,
    ) -> None:
        if lifecycle_state not in {"owned", "terminated", "quarantined", "removed"}:
            raise ValueError("Invalid reconciled container state")
        with self.connect() as connection:
            cursor = connection.execute(
                "UPDATE broker_containers SET lifecycle_state = ?, "
                "reconciliation_evidence = ? WHERE task_id = ?",
                (lifecycle_state, evidence, task_id),
            )
            if cursor.rowcount != 1:
                raise BrokerError("container_not_owned", "No private container ownership record exists.", 404)

    def complete_operation(self, operation_id: str, state: str, result_json: str | None = None) -> None:
        if state not in {"completed", "failed_before_effects", "uncertain"}:
            raise ValueError("Invalid Broker operation terminal state")
        with self.connect() as connection:
            cursor = connection.execute(
                "UPDATE broker_operations SET state = ?, result_json = ?, updated_at = ? "
                "WHERE operation_id = ? AND state IN ('claimed', 'running')",
                (state, result_json, int(time.time()), operation_id),
            )
            if cursor.rowcount != 1:
                raise BrokerError("operation_state_invalid", "Operation is not in a claimable state.", 409)

    def uncertain_operations(self) -> tuple[sqlite3.Row, ...]:
        with self.connect() as connection:
            return tuple(connection.execute(
                "SELECT * FROM broker_operations WHERE state = 'uncertain' "
                "ORDER BY updated_at, operation_id",
            ).fetchall())

    def task(self, task_id: str) -> sqlite3.Row:
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM broker_tasks WHERE task_id = ?", (task_id,)).fetchone()
        if row is None:
            raise BrokerError("task_not_found", "Task identity was not found.", 404)
        return row

    def slot_inventory_matches(self, slot: object) -> bool:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM broker_slots WHERE slot_id = ?", (slot.slot_id,),
            ).fetchone()
        return row is not None and all(row[name] == value for name, value in (
            ("path", str(slot.path)),
            ("filesystem_uuid", slot.filesystem_uuid),
            ("project_id", slot.project_id),
            ("byte_limit", slot.byte_limit),
            ("inode_limit", slot.inode_limit),
        ))

    def storage_role_inventory_matches(self, role: object, generation: str) -> bool:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM broker_storage_roles WHERE role_id = ?", (role.role_id,),
            ).fetchone()
        return row is not None and all(row[name] == value for name, value in (
            ("kind", role.kind),
            ("path", str(role.path)),
            ("mount_path", str(role.mount_path)),
            ("filesystem_uuid", role.filesystem_uuid),
            ("project_id", role.project_id),
            ("byte_limit", role.byte_limit),
            ("inode_limit", role.inode_limit),
            ("owner_uid", role.owner_uid),
            ("owner_gid", role.owner_gid),
            ("policy_generation", generation),
        ))

    def storage_role_configurations(self) -> tuple[sqlite3.Row, ...]:
        with self.connect() as connection:
            return tuple(connection.execute(
                "SELECT * FROM broker_storage_roles ORDER BY role_id",
            ).fetchall())

    def unassigned_legacy_tasks(self) -> tuple[str, ...]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT task_id FROM broker_tasks WHERE slot_id IS NULL AND state != 'removed'",
            ).fetchall()
        return tuple(row["task_id"] for row in rows)

    def task_slot(self, task_id: str, *, allow_quarantined: bool = False) -> dict[str, object]:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT s.*, t.slot_id AS task_slot_id, "
                "t.slot_owner_token_hash AS task_owner_hash, "
                "t.allocation_id AS task_allocation_id "
                "FROM broker_tasks t JOIN broker_slots s ON s.slot_id = t.slot_id "
                "WHERE t.task_id = ?",
                (task_id,),
            ).fetchone()
        allowed_states = {"allocated", "release_pending"}
        if allow_quarantined:
            allowed_states.add("quarantined")
        if (
            row is None or row["state"] not in allowed_states
            or row["task_id"] != task_id
            or row["allocation_id"] != row["task_allocation_id"]
            or row["task_slot_id"] != row["slot_id"]
            or not row["owner_token_hash"]
            or not hmac.compare_digest(row["owner_token_hash"], row["task_owner_hash"] or "")
        ):
            raise BrokerError(
                "workspace_slot_mismatch",
                "Durable task-to-quota-slot ownership is unavailable or quarantined.",
                409,
            )
        return dict(row)

    def quarantine_slot(self, task_id: str) -> None:
        with self.connect() as connection:
            connection.execute(
                "UPDATE broker_slots SET state = 'quarantined', updated_at = ? "
                "WHERE task_id = ? AND state != 'free'",
                (int(time.time()), task_id),
            )

    def quarantine_all_for_storage_loss(self, reason: str) -> tuple[str, ...]:
        if not reason or len(reason) > 128:
            raise BrokerError("storage_failure_invalid", "Storage failure reason is invalid.", 500)
        now = int(time.time())
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                tasks = tuple(row["task_id"] for row in connection.execute(
                    "SELECT task_id FROM broker_slots WHERE task_id IS NOT NULL "
                    "AND state != 'free' ORDER BY task_id",
                ).fetchall())
                connection.execute(
                    "UPDATE broker_operations SET state = 'uncertain', updated_at = ? "
                    "WHERE state IN ('claimed', 'running') AND task_id IN "
                    "(SELECT task_id FROM broker_slots WHERE task_id IS NOT NULL AND state != 'free')",
                    (now,),
                )
                connection.execute(
                    "UPDATE broker_tasks SET state = 'uncertain', updated_at = ? "
                    "WHERE task_id IN (SELECT task_id FROM broker_slots "
                    "WHERE task_id IS NOT NULL AND state != 'free') AND state != 'removed'",
                    (now,),
                )
                connection.execute(
                    "UPDATE broker_slots SET state = 'quarantined', updated_at = ? "
                    "WHERE task_id IS NOT NULL AND state != 'free'",
                    (now,),
                )
                for task_id in tasks:
                    connection.execute(
                        "UPDATE broker_containers SET "
                        "lifecycle_state = CASE "
                        "WHEN lifecycle_state IN ('removed', 'terminated') THEN lifecycle_state "
                        "ELSE 'quarantined' END, "
                        "reconciliation_evidence = CASE "
                        "WHEN lifecycle_state IN ('removed', 'terminated') THEN reconciliation_evidence "
                        "ELSE ? END WHERE task_id = ?",
                        (f"storage_assurance_lost:{reason}:{now}", task_id),
                    )
                connection.commit()
                return tasks
            except BaseException:
                connection.rollback()
                raise

    def has_quarantined_slots(self) -> bool:
        with self.connect() as connection:
            return connection.execute(
                "SELECT 1 FROM broker_slots WHERE state = 'quarantined' LIMIT 1",
            ).fetchone() is not None

    def has_pending_slot_releases(self) -> bool:
        with self.connect() as connection:
            return connection.execute(
                "SELECT 1 FROM broker_slots WHERE state = 'release_pending' LIMIT 1",
            ).fetchone() is not None

    def slot_configurations(self) -> tuple[sqlite3.Row, ...]:
        with self.connect() as connection:
            return tuple(connection.execute(
                "SELECT * FROM broker_slots ORDER BY slot_id",
            ).fetchall())

    def mark_slot_release_pending(self, task_id: str, operation_id: str, evidence: str) -> None:
        self.require_execution_authority()
        if not evidence or len(evidence) > 4096:
            raise BrokerError("slot_release_evidence_invalid", "Quota-slot release evidence is invalid.", 500)
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                task = connection.execute(
                    "SELECT state, slot_id FROM broker_tasks WHERE task_id = ?", (task_id,),
                ).fetchone()
                if task is None or task["state"] != "removed" or not task["slot_id"]:
                    raise BrokerError("slot_release_refused", "Task is not safely terminal for quota-slot release.", 409)
                container = connection.execute(
                    "SELECT lifecycle_state FROM broker_containers WHERE task_id = ?",
                    (task_id,),
                ).fetchone()
                if container is not None and container["lifecycle_state"] != "removed":
                    raise BrokerError("slot_release_refused", "Task container removal is not durably verified.", 409)
                unresolved = connection.execute(
                    "SELECT operation_id, kind, state FROM broker_operations WHERE task_id = ? "
                    "AND state IN ('claimed', 'running', 'uncertain')",
                    (task_id,),
                ).fetchall()
                if (
                    len(unresolved) == 1
                    and unresolved[0]["operation_id"] == operation_id
                    and unresolved[0]["kind"] in {"remove", "cancel"}
                    and unresolved[0]["state"] == "running"
                    and (container is None or unresolved[0]["kind"] == "remove")
                ):
                    cursor = connection.execute(
                        "UPDATE broker_slots SET state = 'release_pending', release_evidence = ?, updated_at = ? "
                        "WHERE slot_id = ? AND task_id = ? AND state = 'allocated'",
                        (evidence, int(time.time()), task["slot_id"], task_id),
                    )
                    if cursor.rowcount != 1:
                        raise BrokerError("slot_release_refused", "Quota-slot ownership changed during cleanup.", 409)
                else:
                    raise BrokerError("slot_release_refused", "Task has unresolved operation provenance.", 409)
                connection.commit()
            except BaseException:
                connection.rollback()
                raise

    def release_slot(self, task_id: str, evidence: str) -> None:
        self.require_execution_authority()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                task = connection.execute(
                    "SELECT state, slot_id FROM broker_tasks WHERE task_id = ?", (task_id,),
                ).fetchone()
                slot = connection.execute(
                    "SELECT * FROM broker_slots WHERE task_id = ?", (task_id,),
                ).fetchone()
                unresolved = connection.execute(
                    "SELECT 1 FROM broker_operations WHERE task_id = ? "
                    "AND state IN ('claimed', 'running', 'uncertain') LIMIT 1",
                    (task_id,),
                ).fetchone()
                container = connection.execute(
                    "SELECT lifecycle_state FROM broker_containers WHERE task_id = ?",
                    (task_id,),
                ).fetchone()
                if (
                    task is None or task["state"] != "removed"
                    or slot is None or slot["state"] != "release_pending"
                    or slot["slot_id"] != task["slot_id"]
                    or unresolved is not None
                    or (container is not None and container["lifecycle_state"] != "removed")
                    or not evidence or len(evidence) > 4096
                ):
                    raise BrokerError("slot_release_refused", "Quota slot is not safe to return to the free pool.", 409)
                connection.execute(
                    "UPDATE broker_slots SET state = 'free', task_id = NULL, allocation_id = NULL, "
                    "owner_token_hash = NULL, release_evidence = ?, updated_at = ? "
                    "WHERE slot_id = ? AND state = 'release_pending'",
                    (evidence, int(time.time()), task["slot_id"]),
                )
                connection.commit()
            except BaseException:
                connection.rollback()
                raise

    def pending_slot_releases(self) -> tuple[sqlite3.Row, ...]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT s.*, t.state AS task_state FROM broker_slots s "
                "JOIN broker_tasks t ON t.task_id = s.task_id "
                "WHERE s.state = 'release_pending' ORDER BY s.updated_at, s.slot_id",
            ).fetchall()
        return tuple(rows)

    def task_has_unresolved_operations(self, task_id: str) -> bool:
        with self.connect() as connection:
            return connection.execute(
                "SELECT 1 FROM broker_operations WHERE task_id = ? "
                "AND state IN ('claimed', 'running', 'uncertain') LIMIT 1",
                (task_id,),
            ).fetchone() is not None

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
