from __future__ import annotations

import hashlib
import hmac
import json
import re
import time
from dataclasses import asdict, dataclass
from enum import StrEnum
from typing import Any, Literal

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey


PROTOCOL_VERSION = 1
MAX_REQUEST_BYTES = 2 * 1024 * 1024
MAX_CLAIM_BYTES = 16 * 1024
_ID = re.compile(r"[a-f0-9]{32}\Z")
_HASH = re.compile(r"[a-f0-9]{64}\Z")


class BrokerError(Exception):
    def __init__(self, code: str, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.public_message = message
        self.status_code = status


class OperationKind(StrEnum):
    PREPARE = "prepare"
    START = "start"
    STATUS = "status"
    READ_FILE = "read_file"
    LIST_FILES = "list_files"
    WRITE_FILE = "write_file"
    PATCH_FILE = "patch_file"
    DELETE_FILE = "delete_file"
    COMMAND = "command"
    VERIFY = "verify"
    COLLECT = "collect"
    COMPLETE = "complete"
    CANCEL = "cancel"
    TERMINATE = "terminate"
    REMOVE = "remove"


TOOL_KINDS = frozenset({
    "read_file", "list_files", "write_file", "patch_file", "delete_file",
    "command", "verify", "collect",
})
LIFECYCLE_KINDS = frozenset({
    "prepare", "start", "status", "complete", "cancel", "terminate", "remove",
})


@dataclass(frozen=True)
class TaskClaim:
    schema_version: int
    task_id: str
    project_id: str
    snapshot_id: str
    snapshot_digest: str
    operation_id: str
    arguments_digest: str
    deadline: int
    target_id: str
    profile: str
    tool_kind: str
    plan_digest: str
    policy_decision_id: str
    policy_version: int
    approval_id: str
    lease_generation: int
    fencing_token: str
    image_digest: str
    resource_profile: str
    issued_at: int

    def validate(self, now: int | None = None) -> None:
        timestamp = int(time.time()) if now is None else now
        for name in ("task_id", "project_id", "snapshot_id", "operation_id", "target_id", "policy_decision_id", "approval_id"):
            if not isinstance(getattr(self, name), str) or not _ID.fullmatch(getattr(self, name)):
                raise BrokerError("claim_invalid", f"Invalid {name}.", 422)
        for name in ("snapshot_digest", "arguments_digest", "plan_digest", "image_digest"):
            value = getattr(self, name)
            if not isinstance(value, str) or not _HASH.fullmatch(value):
                raise BrokerError("claim_invalid", f"Invalid {name}.", 422)
        if (
            type(self.schema_version) is not int or self.schema_version != PROTOCOL_VERSION
            or type(self.deadline) is not int or type(self.issued_at) is not int
            or not self.issued_at <= timestamp < self.deadline
            or self.deadline - self.issued_at > 900
            or not isinstance(self.fencing_token, str) or not 32 <= len(self.fencing_token) <= 128
            or any(ord(ch) < 33 or ord(ch) > 126 for ch in self.fencing_token)
            or type(self.lease_generation) is not int or self.lease_generation < 1
            or type(self.policy_version) is not int or self.policy_version < 1
            or self.tool_kind not in TOOL_KINDS | LIFECYCLE_KINDS
            or not isinstance(self.profile, str) or not 1 <= len(self.profile) <= 32
            or not isinstance(self.resource_profile, str) or not 1 <= len(self.resource_profile) <= 32
        ):
            raise BrokerError("claim_invalid", "Task claim is invalid or expired.", 403)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: object) -> TaskClaim:
        if not isinstance(value, dict) or set(value) != set(cls.__dataclass_fields__):
            raise BrokerError("claim_invalid", "Task claim fields do not match protocol v1.", 422)
        try:
            return cls(**value)
        except TypeError as exc:
            raise BrokerError("claim_invalid", "Task claim fields are invalid.", 422) from exc


def canonical_json(value: object) -> bytes:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise BrokerError("request_invalid", "Request is not canonical JSON data.", 422) from exc


def sign_claim(claim: TaskClaim, key: bytes) -> str:
    claim.validate(now=claim.issued_at)
    body = canonical_json(claim.to_dict())
    if len(body) > MAX_CLAIM_BYTES:
        raise BrokerError("claim_too_large", "Task claim exceeds its size limit.", 413)
    if len(key) != 32:
        raise BrokerError("claim_signing_key_invalid", "Core claim signing key is invalid.", 500)
    return Ed25519PrivateKey.from_private_bytes(key).sign(
        b"synai-broker-claim-v1\n" + body,
    ).hex()


def verify_claim(claim: TaskClaim, signature: str, key: bytes, now: int | None = None) -> None:
    claim.validate(now)
    if not isinstance(signature, str) or not re.fullmatch(r"[a-f0-9]{128}", signature):
        raise BrokerError("claim_auth_invalid", "Task claim authentication failed.", 401)
    if len(key) != 32:
        raise BrokerError("claim_auth_invalid", "Task claim authentication failed.", 401)
    body = canonical_json(claim.to_dict())
    if len(body) > MAX_CLAIM_BYTES:
        raise BrokerError("claim_too_large", "Task claim exceeds its size limit.", 413)
    try:
        Ed25519PublicKey.from_public_bytes(key).verify(
            bytes.fromhex(signature), b"synai-broker-claim-v1\n" + body,
        )
    except (InvalidSignature, ValueError) as exc:
        raise BrokerError("claim_auth_invalid", "Task claim authentication failed.", 401) from exc


def request_mac(method: str, path: str, timestamp: int, nonce: str, body: bytes, key: bytes) -> str:
    if len(body) > MAX_REQUEST_BYTES or not path.startswith("/") or "\n" in path:
        raise BrokerError("request_invalid", "Broker request is outside its size or path limits.", 413)
    payload = b"synai-broker-request-v1\n" + b"\n".join((
        method.upper().encode("ascii"), path.encode("ascii"), str(timestamp).encode("ascii"),
        nonce.encode("ascii"), hashlib.sha256(body).hexdigest().encode("ascii"),
    ))
    return hmac.new(key, payload, hashlib.sha256).hexdigest()


def validate_nonce(nonce: str, timestamp: int, now: int | None = None) -> None:
    current = int(time.time()) if now is None else now
    if (
        not isinstance(nonce, str) or not re.fullmatch(r"[a-f0-9]{32}", nonce)
        or type(timestamp) is not int or abs(current - timestamp) > 30
    ):
        raise BrokerError("request_auth_invalid", "Broker request authentication is invalid or expired.", 401)
