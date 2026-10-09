from __future__ import annotations

import contextlib
import os
import sqlite3
import stat
from collections.abc import Iterator
from pathlib import Path

from synai.storage import ConversationStorage, checked_path


class MetadataDatabase:
    """Private, versioned browser-service metadata, separate from conversation history."""

    SCHEMA_VERSION = 3

    def __init__(self, data_root: Path) -> None:
        self.storage = ConversationStorage(data_root)
        self.directory = checked_path(self.storage.root / "web")
        self.path = checked_path(self.directory / "metadata.sqlite3")

    def initialize(self) -> None:
        self.storage.initialize()
        self._private_directory(self.directory)
        if self.path.exists() or self.path.is_symlink():
            checked_path(self.path)
            info = self.path.stat()
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise ValueError("Web metadata must be a private regular file owned by this user")
        with self.connect() as connection:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version not in {0, 1, 2, self.SCHEMA_VERSION}:
                raise ValueError("Unsupported web metadata schema version")
            if version == 0:
                connection.executescript(
                    """
                    BEGIN EXCLUSIVE;
                    CREATE TABLE credentials (
                        singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                        password_hash TEXT NOT NULL
                    );
                    CREATE TABLE sessions (
                        token_hash TEXT PRIMARY KEY,
                        csrf_hash TEXT NOT NULL,
                        created_at INTEGER NOT NULL,
                        expires_at INTEGER NOT NULL
                    );
                    CREATE INDEX sessions_expiry ON sessions(expires_at);
                    CREATE TABLE login_limits (
                        peer_hash TEXT PRIMARY KEY,
                        window_start INTEGER NOT NULL,
                        attempts INTEGER NOT NULL,
                        blocked_until INTEGER NOT NULL
                    );
                    CREATE INDEX login_limits_age ON login_limits(window_start);
                    CREATE TABLE projects (
                        project_id TEXT PRIMARY KEY,
                        workspace_key TEXT NOT NULL UNIQUE,
                        display_name TEXT NOT NULL,
                        device_id INTEGER NOT NULL,
                        inode INTEGER NOT NULL,
                        owner_uid INTEGER NOT NULL,
                        registered_at INTEGER NOT NULL
                    );
                    CREATE TABLE workspace_lease_generations (
                        workspace_key TEXT NOT NULL,
                        generation INTEGER NOT NULL,
                        owner_id TEXT NOT NULL,
                        workflow_id TEXT NOT NULL,
                        task_id TEXT NOT NULL,
                        label TEXT NOT NULL,
                        token_hash TEXT NOT NULL,
                        acquired_at INTEGER NOT NULL,
                        released_at INTEGER,
                        status TEXT NOT NULL,
                        PRIMARY KEY (workspace_key, generation)
                    );
                    CREATE INDEX lease_latest ON workspace_lease_generations(
                        workspace_key, generation DESC
                    );
                    PRAGMA user_version = 2;
                    COMMIT;
                    """
                )
            elif version == 1:
                connection.execute("BEGIN EXCLUSIVE")
                try:
                    columns = {
                        row["name"]
                        for row in connection.execute(
                            "PRAGMA table_info(workspace_lease_generations)",
                        ).fetchall()
                    }
                    if "label" not in columns:
                        connection.execute(
                            "ALTER TABLE workspace_lease_generations "
                            "ADD COLUMN label TEXT NOT NULL DEFAULT ''",
                        )
                    connection.execute("PRAGMA user_version = 2")
                    connection.commit()
                except BaseException:
                    connection.rollback()
                    raise
            elif version == 2:
                self._migrate_to_distributed_v3(connection)
            current_version = connection.execute("PRAGMA user_version").fetchone()[0]
            if current_version == 2:
                self._migrate_to_distributed_v3(connection)
            elif current_version != self.SCHEMA_VERSION:
                raise ValueError("Web metadata migration did not reach the current schema version")
        os.chmod(self.path, 0o600)

    @staticmethod
    def _migrate_to_distributed_v3(connection: sqlite3.Connection) -> None:
        try:
            connection.executescript(
                """
                BEGIN EXCLUSIVE;
                CREATE TABLE logical_projects (
                    project_id TEXT PRIMARY KEY CHECK(length(project_id) = 32),
                    schema_version INTEGER NOT NULL CHECK(schema_version = 1),
                    registration_key_hash TEXT NOT NULL UNIQUE,
                    display_name TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('active', 'archived'))
                );
                CREATE TABLE pairing_challenges (
                    challenge_id TEXT PRIMARY KEY CHECK(length(challenge_id) = 32),
                    secret_hash TEXT NOT NULL UNIQUE,
                    created_at INTEGER NOT NULL,
                    expires_at INTEGER NOT NULL,
                    consumed_at INTEGER,
                    device_id TEXT
                );
                CREATE INDEX pairing_challenge_expiry ON pairing_challenges(expires_at);
                CREATE TABLE paired_devices (
                    device_id TEXT PRIMARY KEY CHECK(length(device_id) = 32),
                    schema_version INTEGER NOT NULL CHECK(schema_version = 1),
                    public_key BLOB NOT NULL,
                    key_fingerprint TEXT NOT NULL UNIQUE,
                    state TEXT NOT NULL CHECK(state IN ('pending', 'authorized', 'revoked')),
                    protocol_version INTEGER NOT NULL,
                    capabilities_json TEXT NOT NULL,
                    credential_hash TEXT NOT NULL UNIQUE,
                    credential_expires_at INTEGER NOT NULL,
                    credential_generation INTEGER NOT NULL DEFAULT 1,
                    created_at INTEGER NOT NULL,
                    authorized_at INTEGER,
                    revoked_at INTEGER,
                    last_seen_at INTEGER
                );
                CREATE INDEX paired_devices_state ON paired_devices(state, created_at);
                CREATE TABLE workspace_bindings (
                    binding_id TEXT PRIMARY KEY CHECK(length(binding_id) = 32),
                    schema_version INTEGER NOT NULL CHECK(schema_version = 1),
                    project_id TEXT NOT NULL REFERENCES logical_projects(project_id),
                    device_id TEXT NOT NULL REFERENCES paired_devices(device_id),
                    display_name TEXT NOT NULL,
                    state TEXT NOT NULL CHECK(state IN ('active', 'stale', 'revoked')),
                    created_at INTEGER NOT NULL,
                    expires_at INTEGER,
                    revoked_at INTEGER,
                    UNIQUE(project_id, device_id, display_name)
                );
                CREATE INDEX workspace_bindings_owner
                    ON workspace_bindings(project_id, device_id, state);
                CREATE TABLE snapshot_uploads (
                    upload_id TEXT PRIMARY KEY CHECK(length(upload_id) = 32),
                    project_id TEXT NOT NULL REFERENCES logical_projects(project_id),
                    device_id TEXT NOT NULL REFERENCES paired_devices(device_id),
                    binding_id TEXT NOT NULL REFERENCES workspace_bindings(binding_id),
                    idempotency_key_hash TEXT NOT NULL,
                    state TEXT NOT NULL CHECK(state IN
                        ('receiving', 'committing', 'committed', 'expired', 'failed')),
                    manifest_json TEXT NOT NULL,
                    reserved_bytes INTEGER NOT NULL CHECK(reserved_bytes >= 0),
                    created_at INTEGER NOT NULL,
                    deadline INTEGER NOT NULL,
                    snapshot_id TEXT,
                    UNIQUE(device_id, idempotency_key_hash)
                );
                CREATE INDEX snapshot_uploads_active ON snapshot_uploads(state, deadline);
                CREATE TABLE snapshot_upload_chunks (
                    upload_id TEXT NOT NULL REFERENCES snapshot_uploads(upload_id) ON DELETE CASCADE,
                    file_index INTEGER NOT NULL CHECK(file_index >= 0),
                    chunk_index INTEGER NOT NULL CHECK(chunk_index >= 0),
                    byte_length INTEGER NOT NULL CHECK(byte_length >= 0),
                    sha256 TEXT NOT NULL,
                    PRIMARY KEY(upload_id, file_index, chunk_index)
                );
                CREATE TABLE immutable_snapshots (
                    snapshot_id TEXT PRIMARY KEY CHECK(length(snapshot_id) = 32),
                    schema_version INTEGER NOT NULL CHECK(schema_version = 1),
                    project_id TEXT NOT NULL REFERENCES logical_projects(project_id),
                    device_id TEXT NOT NULL REFERENCES paired_devices(device_id),
                    binding_id TEXT NOT NULL REFERENCES workspace_bindings(binding_id),
                    manifest_json TEXT NOT NULL,
                    manifest_digest TEXT NOT NULL,
                    total_bytes INTEGER NOT NULL CHECK(total_bytes >= 0),
                    state TEXT NOT NULL CHECK(state IN ('available', 'expired')),
                    created_at INTEGER NOT NULL,
                    expires_at INTEGER
                );
                CREATE INDEX immutable_snapshots_project
                    ON immutable_snapshots(project_id, created_at DESC);
                CREATE TABLE device_request_nonces (
                    device_id TEXT NOT NULL REFERENCES paired_devices(device_id),
                    nonce TEXT NOT NULL,
                    expires_at INTEGER NOT NULL,
                    PRIMARY KEY(device_id, nonce)
                );
                CREATE INDEX device_request_nonce_expiry ON device_request_nonces(expires_at);
                CREATE TABLE distributed_tasks (
                    task_id TEXT PRIMARY KEY CHECK(length(task_id) = 32),
                    schema_version INTEGER NOT NULL CHECK(schema_version = 1),
                    project_id TEXT NOT NULL REFERENCES logical_projects(project_id),
                    snapshot_id TEXT NOT NULL REFERENCES immutable_snapshots(snapshot_id),
                    device_id TEXT NOT NULL REFERENCES paired_devices(device_id),
                    binding_id TEXT NOT NULL REFERENCES workspace_bindings(binding_id),
                    state TEXT NOT NULL CHECK(state IN
                        ('not_enabled', 'queued', 'claimed', 'completed', 'failed', 'cancelled')),
                    execution_target TEXT NOT NULL,
                    required_capabilities_json TEXT NOT NULL,
                    execution_claim TEXT,
                    fencing_generation INTEGER NOT NULL DEFAULT 0,
                    approval_reference TEXT,
                    result_reference TEXT,
                    error_reference TEXT,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL
                );
                CREATE TABLE memory_associations (
                    association_id TEXT PRIMARY KEY CHECK(length(association_id) = 32),
                    schema_version INTEGER NOT NULL CHECK(schema_version = 1),
                    legacy_identity TEXT NOT NULL,
                    project_id TEXT NOT NULL REFERENCES logical_projects(project_id),
                    status TEXT NOT NULL CHECK(status = 'preview_only'),
                    provenance_json TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    UNIQUE(legacy_identity, project_id)
                );
                PRAGMA user_version = 3;
                COMMIT;
                """
            )
        except BaseException:
            connection.rollback()
            raise

    @contextlib.contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        checked_path(self.directory)
        checked_path(self.path)
        connection = sqlite3.connect(self.path, timeout=2.0, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 2000")
        try:
            yield connection
        finally:
            connection.close()

    @staticmethod
    def _private_directory(path: Path) -> None:
        checked_path(path)
        if path.exists():
            info = path.stat()
            if (
                not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o077
            ):
                raise ValueError("Web metadata directory must be private and owned by this user")
        else:
            try:
                path.mkdir(mode=0o700)
            except FileExistsError:
                MetadataDatabase._private_directory(path)
