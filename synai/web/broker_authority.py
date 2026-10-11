from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import time
from dataclasses import dataclass
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from synai.broker.operations import validate_operation
from synai.broker.protocol import BrokerError, TaskClaim, canonical_json, sign_claim
from synai.broker.snapshots import MAX_FILES, MAX_SNAPSHOT_BYTES, validate_relative_path
from synai.storage import checked_path
from synai.web.database import MetadataDatabase
from synai.web.distributed import DistributedError
from synai.web.snapshots import SnapshotStore


CLAIM_TTL_SECONDS = 60
SNAPSHOT_GRANT_TTL_SECONDS = 60
MAX_RESULT_BYTES = 8 * 1024 * 1024
_ID = re.compile(r"[a-f0-9]{32}\Z")
_HASH = re.compile(r"[a-f0-9]{64}\Z")
_TEST_OPERATIONS = frozenset({
    "prepare", "start", "status", "read_file", "list_files", "write_file",
    "patch_file", "delete_file", "command", "verify", "collect", "complete",
    "cancel", "terminate", "remove",
})


@dataclass(frozen=True)
class IssuedBrokerClaim:
    claim: TaskClaim
    signature: str
    snapshot_grant: str | None


class CoreBrokerAuthority:
    """Durable Core-side test-gated authority for Broker claims and results."""

    def __init__(
        self,
        database: MetadataDatabase,
        snapshots: SnapshotStore,
        signing_key: bytes,
        *,
        deployment_id: str,
        integration_test_token: str | None = None,
    ) -> None:
        if len(signing_key) != 32 or not _ID.fullmatch(deployment_id):
            raise ValueError("Core Broker authority signing identity is invalid")
        self.database = database
        self.snapshots = snapshots
        self.signing_key = signing_key
        self.deployment_id = deployment_id
        self.integration_test_token = integration_test_token
        self._signer = Ed25519PrivateKey.from_private_bytes(signing_key)
        self.public_key = self._signer.public_key().public_bytes(
            serialization.Encoding.Raw,
            serialization.PublicFormat.Raw,
        )

    def authorize_integration_task(
        self,
        task_id: str,
        *,
        target_id: str,
        plan_id: str,
        plan_digest: str,
        policy_id: str,
        policy_version: int,
        decision_digest: str,
        approval_id: str,
        approval_digest: str,
        image_digest: str,
        image_profile_id: str,
        resource_profile_id: str,
        allowed_operations: frozenset[str],
        operator_token: str,
        now: int | None = None,
    ) -> str:
        """Persist a synthetic operator authorization only behind an explicit test gate."""
        if self.integration_test_token is None or not hmac.compare_digest(
            operator_token, self.integration_test_token,
        ):
            raise DistributedError("broker_test_gate_closed", "Broker integration-test authority is disabled.", 403)
        timestamp = int(time.time()) if now is None else now
        for name, value in (
            ("task_id", task_id), ("target_id", target_id), ("plan_id", plan_id),
            ("approval_id", approval_id),
        ):
            if not isinstance(value, str) or not _ID.fullmatch(value):
                raise DistributedError("broker_authorization_invalid", f"{name} is invalid.", 422)
        for name, value in (
            ("plan_digest", plan_digest), ("decision_digest", decision_digest),
            ("approval_digest", approval_digest), ("image_digest", image_digest),
        ):
            if not isinstance(value, str) or not _HASH.fullmatch(value):
                raise DistributedError("broker_authorization_invalid", f"{name} is invalid.", 422)
        if (
            not _ID.fullmatch(policy_id) or type(policy_version) is not int or policy_version < 1
            or not re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", image_profile_id)
            or not re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", resource_profile_id)
            or not isinstance(allowed_operations, frozenset)
            or not allowed_operations or allowed_operations - _TEST_OPERATIONS
        ):
            raise DistributedError("broker_authorization_invalid", "Broker authorization is outside the integration-test profile.", 422)
        allocation_id = secrets.token_hex(16)
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                task = connection.execute(
                    "SELECT t.task_id, t.project_id, t.snapshot_id, t.state, "
                    "s.manifest_digest, s.state AS snapshot_state, s.expires_at "
                    "FROM distributed_tasks t JOIN immutable_snapshots s "
                    "ON s.snapshot_id = t.snapshot_id WHERE t.task_id = ?",
                    (task_id,),
                ).fetchone()
                if task is None or task["state"] != "not_enabled":
                    raise DistributedError("task_not_authorizable", "Only an existing disabled task contract can be authorized.", 409)
                if task["snapshot_state"] != "available" or task["expires_at"] <= timestamp:
                    raise DistributedError("snapshot_not_found", "Exact task snapshot is unavailable.", 410)
                connection.execute(
                    "INSERT INTO broker_task_authorizations "
                    "(task_id, project_id, snapshot_id, snapshot_digest, allocation_id, target_id, "
                    "plan_id, plan_digest, policy_id, policy_version, decision_digest, approval_id, "
                    "approval_digest, image_digest, image_profile_id, resource_profile_id, "
                    "allowed_operations_json, authorization_kind, authorized_at, expires_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'integration_test_operator', ?, ?)",
                    (
                        task_id, task["project_id"], task["snapshot_id"],
                        task["manifest_digest"], allocation_id, target_id, plan_id,
                        plan_digest, policy_id, policy_version, decision_digest,
                        approval_id, approval_digest, image_digest, image_profile_id,
                        resource_profile_id, canonical_json(sorted(allowed_operations)).decode("utf-8"),
                        timestamp, timestamp + 3600,
                    ),
                )
                lease_token = secrets.token_urlsafe(32)
                connection.execute(
                    "INSERT INTO broker_task_leases "
                    "(task_id, generation, fencing_token, fencing_token_hash, allocation_id, issued_at, expires_at) "
                    "VALUES (?, 1, ?, ?, ?, ?, ?)",
                    (
                        task_id, lease_token,
                        hashlib.sha256(lease_token.encode("ascii")).hexdigest(),
                        allocation_id, timestamp, timestamp + 3600,
                    ),
                )
                connection.execute(
                    "UPDATE distributed_tasks SET state = 'queued', execution_target = ?, "
                    "fencing_generation = 1, approval_reference = ?, updated_at = ? "
                    "WHERE task_id = ? AND state = 'not_enabled'",
                    (target_id, approval_id, timestamp, task_id),
                )
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        return allocation_id

    def issue_claim(
        self,
        task_id: str,
        operation_id: str,
        kind: str,
        arguments: dict[str, Any],
        *,
        now: int | None = None,
    ) -> IssuedBrokerClaim:
        timestamp = int(time.time()) if now is None else now
        if not _ID.fullmatch(task_id) or not _ID.fullmatch(operation_id):
            raise DistributedError("broker_claim_invalid", "Task or operation identity is invalid.", 422)
        try:
            validated_arguments = validate_operation(kind, arguments)
        except BrokerError as exc:
            raise DistributedError("broker_claim_invalid", "Operation arguments are invalid.", 422) from exc
        argument_digest = hashlib.sha256(canonical_json(validated_arguments)).hexdigest()
        grant: str | None = None
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                record = connection.execute(
                    "SELECT a.*, l.generation, l.fencing_token, l.fencing_token_hash, l.expires_at AS lease_expires, "
                    "l.revoked_at AS lease_revoked, t.fencing_generation "
                    "FROM broker_task_authorizations a JOIN broker_task_leases l USING(task_id) "
                    "JOIN distributed_tasks t USING(task_id) WHERE a.task_id = ?",
                    (task_id,),
                ).fetchone()
                if (
                    record is None or record["revoked_at"] is not None
                    or record["lease_revoked"] is not None or record["expires_at"] <= timestamp
                    or record["lease_expires"] <= timestamp
                    or record["generation"] != record["fencing_generation"]
                ):
                    raise DistributedError("broker_authority_stale", "Task authorization or lease is stale.", 409)
                previous = connection.execute(
                    "SELECT claim_digest, claim_json, signature FROM broker_claims "
                    "WHERE operation_id = ?",
                    (operation_id,),
                ).fetchone()
                if previous is not None:
                    raise DistributedError("broker_operation_replayed", "Operation identity has already been issued.", 409)
                allowed = json.loads(record["allowed_operations_json"])
                if kind not in allowed:
                    raise DistributedError("broker_operation_denied", "Operation is not in the persisted authorization.", 403)
                if kind not in _TEST_OPERATIONS:
                    raise DistributedError("broker_operation_denied", "Operation kind is not supported.", 403)
                fencing_token = record["fencing_token"]
                if not hmac.compare_digest(
                    hashlib.sha256(fencing_token.encode("ascii")).hexdigest(),
                    record["fencing_token_hash"],
                ):
                    raise DistributedError("broker_authority_stale", "Task fencing identity is invalid.", 409)
                claim = TaskClaim(
                    schema_version=1, task_id=task_id,
                    project_id=record["project_id"], snapshot_id=record["snapshot_id"],
                    snapshot_digest=record["snapshot_digest"], operation_id=operation_id,
                    arguments_digest=argument_digest, deadline=min(timestamp + CLAIM_TTL_SECONDS, record["expires_at"]),
                    target_id=record["target_id"], profile=record["resource_profile_id"],
                    tool_kind=kind, plan_digest=record["plan_digest"],
                    policy_decision_id=record["decision_digest"][:32],
                    policy_version=record["policy_version"], approval_id=record["approval_id"],
                    lease_generation=record["generation"], fencing_token=fencing_token,
                    image_digest=record["image_digest"], resource_profile=record["resource_profile_id"],
                    issued_at=timestamp, claim_id=secrets.token_hex(16),
                    issuer=f"synai-core:{self.deployment_id}",
                    audience=f"synai-broker:{record['allocation_id']}",
                    allocation_id=record["allocation_id"], plan_id=record["plan_id"],
                    policy_id=record["policy_id"], decision_digest=record["decision_digest"],
                    approval_digest=record["approval_digest"], replay_nonce=secrets.token_hex(16),
                    request_id=secrets.token_hex(16),
                )
                signature = sign_claim(claim, self.signing_key)
                serialized = canonical_json(claim.to_dict()).decode("utf-8")
                claim_digest = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
                connection.execute(
                    "INSERT INTO broker_claims "
                    "(claim_id, operation_id, task_id, project_id, snapshot_id, allocation_id, "
                    "generation, claim_digest, claim_json, signature, state, issued_at, expires_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'issued', ?, ?)",
                    (
                        claim.claim_id, operation_id, task_id, claim.project_id,
                        claim.snapshot_id, claim.allocation_id, claim.lease_generation,
                        claim_digest, serialized, signature, timestamp, claim.deadline,
                    ),
                )
                connection.execute(
                    "INSERT INTO broker_operation_provenance "
                    "(operation_id, claim_id, task_id, project_id, snapshot_id, claim_digest, "
                    "state, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, 'issued', ?, ?)",
                    (
                        operation_id, claim.claim_id, task_id, claim.project_id,
                        claim.snapshot_id, claim_digest, timestamp, timestamp,
                    ),
                )
                if kind == "prepare":
                    grant = secrets.token_urlsafe(32)
                    grant_id = secrets.token_hex(16)
                    grant_nonce = secrets.token_hex(16)
                    connection.execute(
                        "INSERT INTO broker_snapshot_grants "
                        "(grant_id, claim_id, task_id, project_id, snapshot_id, snapshot_digest, "
                        "allocation_id, token_hash, replay_nonce, max_files, max_bytes, issued_at, expires_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            grant_id, claim.claim_id, task_id, claim.project_id,
                            claim.snapshot_id, claim.snapshot_digest, claim.allocation_id,
                            hashlib.sha256(grant.encode("ascii")).hexdigest(), grant_nonce,
                            MAX_FILES, MAX_SNAPSHOT_BYTES, timestamp,
                            min(claim.deadline, timestamp + SNAPSHOT_GRANT_TTL_SECONDS),
                        ),
                    )
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        return IssuedBrokerClaim(claim, signature, grant)

    def validate_live_claim(
        self,
        claim: TaskClaim,
        signature: str,
        *,
        now: int | None = None,
    ) -> bool:
        timestamp = int(time.time()) if now is None else now
        from synai.broker.protocol import verify_claim

        verify_claim(claim, signature, self.public_key, timestamp)
        serialized = canonical_json(claim.to_dict()).decode("utf-8")
        digest = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT c.claim_digest, c.signature, c.state, c.expires_at, "
                "a.revoked_at AS auth_revoked, a.expires_at AS auth_expires, "
                "l.generation, l.fencing_token_hash, l.expires_at AS lease_expires, "
                "l.revoked_at AS lease_revoked FROM broker_claims c "
                "JOIN broker_task_authorizations a USING(task_id) "
                "JOIN broker_task_leases l USING(task_id) WHERE c.claim_id = ? "
                "AND c.operation_id = ? AND c.task_id = ? AND c.project_id = ? "
                "AND c.snapshot_id = ? AND c.allocation_id = ?",
                (
                    claim.claim_id, claim.operation_id, claim.task_id,
                    claim.project_id, claim.snapshot_id, claim.allocation_id,
                ),
            ).fetchone()
        if row is None:
            return False
        token_hash = hashlib.sha256(claim.fencing_token.encode("ascii")).hexdigest()
        return bool(
            row["state"] in {"issued", "running"}
            and row["expires_at"] > timestamp and row["auth_expires"] > timestamp
            and row["lease_expires"] > timestamp
            and row["auth_revoked"] is None and row["lease_revoked"] is None
            and row["generation"] == claim.lease_generation
            and hmac.compare_digest(row["fencing_token_hash"], token_hash)
            and hmac.compare_digest(row["claim_digest"], digest)
            and hmac.compare_digest(row["signature"], signature)
        )

    def mark_operation_running(
        self,
        claim: TaskClaim,
        signature: str,
        *,
        now: int | None = None,
    ) -> None:
        timestamp = int(time.time()) if now is None else now
        from synai.broker.protocol import verify_claim

        verify_claim(claim, signature, self.public_key, timestamp)
        serialized = canonical_json(claim.to_dict()).decode("utf-8")
        digest = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
        token_hash = hashlib.sha256(claim.fencing_token.encode("ascii")).hexdigest()
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT c.state, c.claim_digest, c.signature, c.expires_at, "
                    "a.revoked_at AS auth_revoked, a.expires_at AS auth_expires, "
                    "l.generation, l.fencing_token_hash, l.expires_at AS lease_expires, "
                    "l.revoked_at AS lease_revoked FROM broker_claims c "
                    "JOIN broker_task_authorizations a USING(task_id) "
                    "JOIN broker_task_leases l USING(task_id) "
                    "WHERE c.claim_id = ? AND c.operation_id = ? AND c.task_id = ? "
                    "AND c.project_id = ? AND c.snapshot_id = ? AND c.allocation_id = ?",
                    (
                        claim.claim_id, claim.operation_id, claim.task_id,
                        claim.project_id, claim.snapshot_id, claim.allocation_id,
                    ),
                ).fetchone()
                if (
                    row is None or row["state"] != "issued"
                    or row["expires_at"] <= timestamp or row["auth_expires"] <= timestamp
                    or row["lease_expires"] <= timestamp
                    or row["auth_revoked"] is not None or row["lease_revoked"] is not None
                    or row["generation"] != claim.lease_generation
                    or not hmac.compare_digest(row["fencing_token_hash"], token_hash)
                    or not hmac.compare_digest(row["claim_digest"], digest)
                    or not hmac.compare_digest(row["signature"], signature)
                ):
                    raise DistributedError("broker_authority_stale", "Operation cannot start under current Core authority.", 409)
                connection.execute(
                    "UPDATE broker_claims SET state = 'running' WHERE claim_id = ? AND state = 'issued'",
                    (claim.claim_id,),
                )
                connection.execute(
                    "UPDATE broker_operation_provenance SET state = 'running', updated_at = ? "
                    "WHERE operation_id = ? AND state = 'issued'",
                    (timestamp, claim.operation_id),
                )
                connection.commit()
            except BaseException:
                connection.rollback()
                raise

    def fetch_exact_snapshot(
        self,
        claim: TaskClaim,
        signature: str,
        grant_token: str,
        *,
        now: int | None = None,
    ) -> tuple[dict[str, Any], dict[str, bytes]]:
        timestamp = int(time.time()) if now is None else now
        if not self.validate_live_claim(claim, signature, now=timestamp):
            raise DistributedError("broker_authority_stale", "Snapshot claim is no longer authorized.", 403)
        grant_hash = hashlib.sha256(grant_token.encode("ascii")).hexdigest()
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                grant = connection.execute(
                    "SELECT g.* FROM broker_snapshot_grants g "
                    "JOIN broker_claims c USING(claim_id) "
                    "JOIN broker_task_authorizations a USING(task_id) "
                    "JOIN broker_task_leases l USING(task_id) "
                    "WHERE g.claim_id = ? AND g.task_id = ? "
                    "AND g.project_id = ? AND g.snapshot_id = ? AND g.snapshot_digest = ? "
                    "AND g.allocation_id = ? AND c.state = 'running' AND c.expires_at > ? "
                    "AND a.revoked_at IS NULL AND a.expires_at > ? "
                    "AND l.revoked_at IS NULL AND l.expires_at > ? "
                    "AND l.generation = ? AND l.fencing_token_hash = ?",
                    (
                        claim.claim_id, claim.task_id, claim.project_id,
                        claim.snapshot_id, claim.snapshot_digest, claim.allocation_id,
                        timestamp, timestamp, timestamp, claim.lease_generation,
                        hashlib.sha256(claim.fencing_token.encode("ascii")).hexdigest(),
                    ),
                ).fetchone()
                if (
                    grant is None or grant["revoked_at"] is not None
                    or grant["expires_at"] <= timestamp or grant["consumed_at"] is not None
                    or not hmac.compare_digest(grant["token_hash"], grant_hash)
                ):
                    raise DistributedError("snapshot_grant_invalid", "Exact snapshot grant is invalid, expired, or consumed.", 403)
                connection.execute(
                    "UPDATE broker_snapshot_grants SET consumed_at = ? WHERE grant_id = ? AND consumed_at IS NULL",
                    (timestamp, grant["grant_id"]),
                )
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        status = self.snapshots.snapshot_status(claim.project_id, claim.snapshot_id, now=timestamp)
        if status["manifest_digest"] != claim.snapshot_digest:
            raise DistributedError("snapshot_integrity_error", "Core snapshot digest differs from the signed claim.", 409)
        manifest = status["manifest"]
        snapshot_dir = checked_path(self.snapshots.snapshots / claim.snapshot_id / "objects")
        files: dict[str, bytes] = {}
        for index, entry in enumerate(manifest["files"]):
            if index >= grant["max_files"]:
                raise DistributedError("snapshot_limit", "Snapshot exceeds its task grant.", 413)
            object_path = checked_path(snapshot_dir / f"{index:04d}")
            data = self.snapshots._read_private_file(object_path, self.snapshots.limits.max_file_bytes)
            if (
                len(data) != entry["size_bytes"]
                or hashlib.sha256(data).hexdigest() != entry["sha256"]
                or sum(map(len, files.values())) + len(data) > grant["max_bytes"]
            ):
                raise DistributedError("snapshot_integrity_error", "Immutable snapshot object failed task-grant validation.", 409)
            files[entry["path"]] = data
        return manifest, files

    def revoke_task(self, task_id: str, *, now: int | None = None) -> None:
        timestamp = int(time.time()) if now is None else now
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                cursor = connection.execute(
                    "UPDATE broker_task_authorizations SET revoked_at = ? "
                    "WHERE task_id = ? AND revoked_at IS NULL",
                    (timestamp, task_id),
                )
                if cursor.rowcount != 1:
                    raise DistributedError("broker_task_not_authorized", "Task authorization was not found or already revoked.", 404)
                connection.execute(
                    "UPDATE broker_task_leases SET revoked_at = ? WHERE task_id = ? AND revoked_at IS NULL",
                    (timestamp, task_id),
                )
                connection.execute(
                    "UPDATE broker_snapshot_grants SET revoked_at = ? WHERE task_id = ? AND revoked_at IS NULL",
                    (timestamp, task_id),
                )
                connection.execute(
                    "UPDATE broker_claims SET state = 'revoked', revoked_at = ? "
                    "WHERE task_id = ? AND state IN ('issued', 'running')",
                    (timestamp, task_id),
                )
                connection.execute(
                    "UPDATE broker_operation_provenance SET state = 'revoked', updated_at = ? "
                    "WHERE task_id = ? AND state IN ('issued', 'running')",
                    (timestamp, task_id),
                )
                connection.commit()
            except BaseException:
                connection.rollback()
                raise

    def advance_fence(self, task_id: str, *, now: int | None = None) -> int:
        timestamp = int(time.time()) if now is None else now
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                active = connection.execute(
                    "SELECT count(*) AS count FROM broker_claims "
                    "WHERE task_id = ? AND state IN ('issued', 'running', 'uncertain')",
                    (task_id,),
                ).fetchone()["count"]
                lease = connection.execute(
                    "SELECT generation, expires_at, revoked_at FROM broker_task_leases WHERE task_id = ?",
                    (task_id,),
                ).fetchone()
                if lease is None or lease["revoked_at"] is not None or lease["expires_at"] <= timestamp:
                    raise DistributedError("broker_authority_stale", "Task lease cannot be renewed.", 409)
                if active:
                    raise DistributedError("broker_writer_not_fenced", "A live or uncertain operation prevents fencing transfer.", 409)
                generation = lease["generation"] + 1
                token = secrets.token_urlsafe(32)
                connection.execute(
                    "UPDATE broker_task_leases SET generation = ?, fencing_token = ?, "
                    "fencing_token_hash = ?, issued_at = ?, expires_at = ? WHERE task_id = ?",
                    (
                        generation, token, hashlib.sha256(token.encode("ascii")).hexdigest(),
                        timestamp, timestamp + 3600, task_id,
                    ),
                )
                connection.execute(
                    "UPDATE distributed_tasks SET fencing_generation = ?, updated_at = ? WHERE task_id = ?",
                    (generation, timestamp, task_id),
                )
                connection.commit()
                return generation
            except BaseException:
                connection.rollback()
                raise

    def record_operation_state(
        self,
        claim: TaskClaim,
        signature: str,
        state: str,
        *,
        outcome: dict[str, Any] | None = None,
        now: int | None = None,
    ) -> None:
        if state not in {"failed", "uncertain"}:
            raise DistributedError("broker_operation_state_invalid", "Operation state is invalid.", 422)
        timestamp = int(time.time()) if now is None else now
        from synai.broker.protocol import verify_claim
        verify_claim(claim, signature, self.public_key, claim.issued_at)
        claim_digest = hashlib.sha256(canonical_json(claim.to_dict())).hexdigest()
        serialized = None if outcome is None else canonical_json(outcome).decode("utf-8")
        digest = None if serialized is None else hashlib.sha256(serialized.encode("utf-8")).hexdigest()
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                cursor = connection.execute(
                    "UPDATE broker_claims SET state = ? WHERE claim_id = ? AND operation_id = ? "
                    "AND task_id = ? AND claim_digest = ? AND signature = ? "
                    "AND state IN ('issued', 'running')",
                    (
                        state, claim.claim_id, claim.operation_id, claim.task_id,
                        claim_digest, signature,
                    ),
                )
                if cursor.rowcount != 1:
                    raise DistributedError("broker_operation_state_invalid", "Operation state transition is not authorized.", 409)
                connection.execute(
                    "UPDATE broker_operation_provenance SET state = ?, outcome_json = coalesce(?, outcome_json), "
                    "outcome_digest = coalesce(?, outcome_digest), updated_at = ? WHERE operation_id = ?",
                    (state, serialized, digest, timestamp, claim.operation_id),
                )
                connection.commit()
            except BaseException:
                connection.rollback()
                raise

    def validate_and_record_result(
        self,
        claim: TaskClaim,
        signature: str,
        envelope: dict[str, Any],
        *,
        now: int | None = None,
    ) -> dict[str, Any]:
        timestamp = int(time.time()) if now is None else now
        from synai.broker.protocol import verify_claim
        verify_claim(claim, signature, self.public_key, timestamp)
        if not isinstance(envelope, dict) or len(canonical_json(envelope)) > MAX_RESULT_BYTES:
            raise DistributedError("broker_result_invalid", "Broker result exceeds its bound.", 413)
        expected = {
            "schema_version": 1, "claim_id": claim.claim_id,
            "operation_id": claim.operation_id, "task_id": claim.task_id,
            "project_id": claim.project_id, "snapshot_id": claim.snapshot_id,
            "snapshot_digest": claim.snapshot_digest, "allocation_id": claim.allocation_id,
            "lease_generation": claim.lease_generation, "status": "completed",
            "result": envelope.get("result"),
        }
        if any(envelope.get(key) != value for key, value in expected.items()):
            raise DistributedError("broker_result_binding_mismatch", "Broker result does not match its signed claim.", 409)
        if not isinstance(envelope["result"], dict):
            raise DistributedError("broker_result_invalid", "Broker result payload is invalid.", 422)
        status = self.snapshots.snapshot_status(claim.project_id, claim.snapshot_id, now=timestamp)
        manifest = status["manifest"]
        self._validate_changed_files(envelope["result"], manifest)
        serialized = canonical_json(envelope)
        digest = hashlib.sha256(serialized).hexdigest()
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT c.state, c.claim_digest FROM broker_claims c "
                    "JOIN broker_task_authorizations a USING(task_id) "
                    "JOIN broker_task_leases l USING(task_id) "
                    "WHERE c.claim_id = ? AND c.operation_id = ? AND c.task_id = ? "
                    "AND c.project_id = ? AND c.snapshot_id = ? AND c.allocation_id = ? "
                    "AND c.expires_at > ? AND a.revoked_at IS NULL AND a.expires_at > ? "
                    "AND l.revoked_at IS NULL AND l.expires_at > ? AND l.generation = ? "
                    "AND l.fencing_token_hash = ?",
                    (
                        claim.claim_id, claim.operation_id, claim.task_id,
                        claim.project_id, claim.snapshot_id, claim.allocation_id,
                        timestamp, timestamp, timestamp, claim.lease_generation,
                        hashlib.sha256(claim.fencing_token.encode("ascii")).hexdigest(),
                    ),
                ).fetchone()
                if row is None or row["state"] not in {"issued", "running"}:
                    raise DistributedError("broker_result_state_invalid", "Broker operation is not in a result-accepting state.", 409)
                if connection.execute(
                    "SELECT 1 FROM broker_task_results WHERE operation_id = ?",
                    (claim.operation_id,),
                ).fetchone():
                    raise DistributedError("broker_result_replayed", "Broker result was already committed.", 409)
                connection.execute(
                    "INSERT INTO broker_task_results "
                    "(operation_id, claim_id, task_id, project_id, snapshot_id, snapshot_digest, "
                    "result_json, result_digest, validated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        claim.operation_id, claim.claim_id, claim.task_id, claim.project_id,
                        claim.snapshot_id, claim.snapshot_digest, serialized.decode("utf-8"),
                        digest, timestamp,
                    ),
                )
                connection.execute(
                    "UPDATE broker_claims SET state = 'completed' WHERE claim_id = ?",
                    (claim.claim_id,),
                )
                connection.execute(
                    "UPDATE broker_operation_provenance SET state = 'completed', outcome_json = ?, "
                    "outcome_digest = ?, updated_at = ? WHERE operation_id = ?",
                    (serialized.decode("utf-8"), digest, timestamp, claim.operation_id),
                )
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        return {"operation_id": claim.operation_id, "result_digest": digest, "validated": True}

    @staticmethod
    def _validate_changed_files(result: dict[str, Any], manifest: dict[str, Any]) -> None:
        changes = result.get("changed_files", [])
        if (
            not isinstance(changes, list) or len(changes) > MAX_FILES
            or result.get("collection_complete", True) is not True
        ):
            raise DistributedError("broker_result_invalid", "Changed-file evidence is outside its bounds.", 422)
        declared_manifest_digest = result.get("artifact_manifest_sha256")
        actual_manifest_digest = hashlib.sha256(canonical_json(changes)).hexdigest()
        if changes and declared_manifest_digest != actual_manifest_digest:
            raise DistributedError("broker_result_invalid", "Changed-file manifest digest is invalid.", 409)
        original = {entry["path"]: entry for entry in manifest["files"]}
        seen: set[str] = set()
        total = 0
        for change in changes:
            if not isinstance(change, dict) or not {
                "path", "preimage_sha256", "postimage_sha256", "size_bytes",
            }.issubset(change) or set(change) - {
                "path", "preimage_sha256", "postimage_sha256", "size_bytes",
                "artifact_base64",
            }:
                raise DistributedError("broker_result_invalid", "Changed-file manifest shape is invalid.", 422)
            path = validate_relative_path(change["path"])
            if path in seen:
                raise DistributedError("broker_result_invalid", "Changed-file manifest contains duplicate paths.", 422)
            seen.add(path)
            old = original.get(path)
            expected_preimage = None if old is None else old["sha256"]
            if change["preimage_sha256"] != expected_preimage:
                raise DistributedError("broker_result_preimage_mismatch", "Changed-file preimage does not match the immutable snapshot.", 409)
            postimage = change["postimage_sha256"]
            size = change["size_bytes"]
            if postimage is None:
                if old is None or "artifact_base64" in change or size != 0:
                    raise DistributedError("broker_result_invalid", "Deletion evidence is invalid.", 422)
                continue
            if not isinstance(postimage, str) or not _HASH.fullmatch(postimage) or type(size) is not int or not 0 <= size <= 1024 * 1024:
                raise DistributedError("broker_result_invalid", "Postimage evidence is invalid.", 422)
            encoded = change.get("artifact_base64")
            if not isinstance(encoded, str):
                raise DistributedError("broker_result_artifact_missing", "Changed-file content is missing.", 422)
            try:
                content = base64.b64decode(encoded, validate=True)
            except ValueError as exc:
                raise DistributedError("broker_result_artifact_invalid", "Changed-file content encoding is invalid.", 422) from exc
            if len(content) != size or hashlib.sha256(content).hexdigest() != postimage:
                raise DistributedError("broker_result_artifact_invalid", "Changed-file bytes do not match their claimed digest.", 409)
            total += len(content)
            if total > MAX_SNAPSHOT_BYTES:
                raise DistributedError("broker_result_invalid", "Changed-file evidence exceeds its aggregate limit.", 413)

    def _lease_token(self, task_id: str, generation: int, issued_at: int) -> str:
        del task_id, generation, issued_at
        raise RuntimeError("Fencing tokens are read only from the durable Core lease ledger")
