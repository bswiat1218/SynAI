from __future__ import annotations

import json
import math
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Any
from uuid import uuid4


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _text(value: object, label: str, *, optional: bool = False, limit: int = 16_384) -> None:
    if optional and value is None:
        return
    if not isinstance(value, str) or not value or len(value) > limit:
        raise ValueError(f"Invalid agent {label}")


def _timestamp(value: object, label: str) -> None:
    if not isinstance(value, str):
        raise ValueError(f"Invalid agent {label}")
    _text(value, label, limit=64)
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"Invalid agent {label}") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"Invalid agent {label}")


def _object(value: object, label: str, keys: set[str]) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        raise ValueError(f"Invalid agent {label} fields")
    return value


class AgentStatus(StrEnum):
    IDLE = "idle"
    UNDERSTANDING = "understanding"
    PLANNING = "planning"
    CONTEXT_GATHERING = "context_gathering"
    IMPLEMENTING = "implementing"
    VERIFYING = "verifying"
    REVIEWING = "reviewing"
    REPAIRING = "repairing"
    WAITING_FOR_APPROVAL = "waiting_for_approval"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"


class StepStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"
    BLOCKED = "blocked"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"


class ExecutionStatus(StrEnum):
    PENDING = "pending"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    DENIED = "denied"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"


class ApprovalStatus(StrEnum):
    NOT_REQUIRED = "not_required"
    PENDING = "pending"
    APPROVED = "approved"
    DENIED = "denied"


class AgentErrorType(StrEnum):
    MODEL_ERROR = "model_error"
    TOOL_ERROR = "tool_error"
    VALIDATION_ERROR = "validation_error"
    VERIFICATION_FAILURE = "verification_failure"
    REPAIR_FAILURE = "repair_failure"
    PERMISSION_DENIED = "permission_denied"
    SANDBOX_ERROR = "sandbox_error"
    CANCELLATION = "cancellation"
    TIMEOUT = "timeout"
    CONTEXT_ERROR = "context_error"
    INTERRUPTED = "interrupted"


class VerificationStatus(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    UNAVAILABLE = "unavailable"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"
    ERROR = "error"
    BLOCKED = "blocked"
    SKIPPED = "skipped"
    RUNNING = "running"
    INTERRUPTED = "interrupted"


class VerificationOutcome(StrEnum):
    PASSED = "passed"
    CODE_FAILURE = "code_failure"
    BLOCKED = "blocked"
    ERROR = "error"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"
    NO_VERIFICATION_NEEDED = "no_verification_needed"


class Repairability(StrEnum):
    CODE_REPAIR_CANDIDATE = "code_repair_candidate"
    CONFIGURATION_REPAIR_CANDIDATE = "configuration_repair_candidate"
    NOT_REPAIRABLE = "not_repairable"
    UNKNOWN = "unknown"


class RepairStatus(StrEnum):
    PENDING = "pending"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    EXHAUSTED = "exhausted"
    BLOCKED = "blocked"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"
    REPLAN_REQUIRED = "replan_required"
    MUTATION_NOT_PERFORMED = "mutation_not_performed"


class RepairOutcome(StrEnum):
    REPAIRED_PENDING_VERIFICATION = "repaired_pending_verification"
    REPAIR_FAILED = "repair_failed"
    REPAIR_BLOCKED = "repair_blocked"
    REPAIR_CANCELLED = "repair_cancelled"
    REPLAN_REQUIRED = "replan_required"
    REPAIR_SCOPE_VIOLATION = "repair_scope_violation"
    REPAIR_MUTATION_NOT_PERFORMED = "repair_mutation_not_performed"
    REPAIR_RESOURCE_LIMIT = "repair_resource_limit"
    REPAIR_ATTEMPTS_EXHAUSTED = "repair_attempts_exhausted"
    REPAIR_PROVIDER_ERROR = "repair_provider_error"
    VERIFICATION_PASSED = "verification_passed"
    VERIFICATION_BLOCKED = "verification_blocked"
    VERIFICATION_ERROR = "verification_error"
    REPAIR_INTERRUPTED = "repair_interrupted"


class ReviewCategory(StrEnum):
    CORRECTNESS = "correctness"
    REGRESSION_RISK = "regression_risk"
    SECURITY = "security"
    PLAN_ALIGNMENT = "plan_alignment"
    TEST_INTEGRITY = "test_integrity"
    ARCHITECTURE_CONSISTENCY = "architecture_consistency"
    MAINTAINABILITY = "maintainability"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"


class ReviewSeverity(StrEnum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    INFO = "info"


class ReviewConfidence(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class ReviewOutcome(StrEnum):
    PASSED = "passed"
    PASSED_WITH_WARNINGS = "passed_with_warnings"
    CHANGES_REQUESTED = "changes_requested"
    BLOCKED = "blocked"
    ERROR = "error"
    CANCELLED = "cancelled"


class PlanOperation(StrEnum):
    READ = "read"
    SEARCH = "search"
    CREATE = "create"
    MODIFY = "modify"
    DELETE = "delete"
    TEST = "test"
    VERIFY = "verify"
    DOCUMENT = "document"


class VerificationIntent(StrEnum):
    TARGETED_TESTS = "targeted_tests"
    RELEVANT_TESTS = "relevant_tests"
    FULL_TEST_SUITE = "full_test_suite"
    LINT = "lint"
    TYPE_CHECK = "type_check"
    BUILD = "build"
    SYNTAX_CHECK = "syntax_check"


@dataclass
class VerificationCheck:
    check_id: str
    intent: VerificationIntent
    project_type: str
    display_name: str
    argv: tuple[str, ...]
    cwd: str
    relevant_paths: tuple[str, ...]
    required: bool
    available: bool
    timeout_seconds: float
    output_limit: int
    discovery_reason: str

    def validate(self) -> None:
        _text(self.check_id, "verification check ID", limit=128)
        if not re.fullmatch(r"check-[a-z0-9]+(?:-[a-z0-9]+)*", self.check_id):
            raise ValueError("Invalid verification check ID")
        if not isinstance(self.intent, VerificationIntent):
            raise ValueError("Invalid verification intent")
        _text(self.project_type, "verification project type", limit=64)
        _text(self.display_name, "verification display name", limit=256)
        if (
            not isinstance(self.argv, tuple) or len(self.argv) > 64
            or any(not isinstance(value, str) or not value or len(value) > 4096 for value in self.argv)
            or self.available and not self.argv
            or not self.available and self.argv
        ):
            raise ValueError("Invalid verification argv")
        if not _safe_verification_path(self.cwd, allow_root=True):
            raise ValueError("Invalid verification working directory")
        if (
            not isinstance(self.relevant_paths, tuple) or len(self.relevant_paths) > 64
            or any(not _safe_verification_path(path) for path in self.relevant_paths)
            or len(set(self.relevant_paths)) != len(self.relevant_paths)
        ):
            raise ValueError("Invalid verification relevant paths")
        if type(self.required) is not bool:
            raise ValueError("Invalid verification check required flag")
        if type(self.available) is not bool:
            raise ValueError("Invalid verification check availability")
        if (
            isinstance(self.timeout_seconds, bool)
            or not isinstance(self.timeout_seconds, (int, float))
            or not math.isfinite(self.timeout_seconds)
            or not 0 < self.timeout_seconds <= 86_400
        ):
            raise ValueError("Invalid verification timeout")
        if type(self.output_limit) is not int or not 1 <= self.output_limit <= 4_194_304:
            raise ValueError("Invalid verification output limit")
        _text(self.discovery_reason, "verification discovery reason", limit=1024)

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        result = asdict(self)
        result["intent"] = self.intent.value
        result["argv"] = list(self.argv)
        result["relevant_paths"] = list(self.relevant_paths)
        return result

    @classmethod
    def from_dict(cls, value: object) -> VerificationCheck:
        data = _object(value, "verification check", {
            "check_id", "intent", "project_type", "display_name", "argv", "cwd",
            "relevant_paths", "required", "available", "timeout_seconds", "output_limit",
            "discovery_reason",
        })
        if not isinstance(data["argv"], list) or not isinstance(data["relevant_paths"], list):
            raise ValueError("Invalid verification check sequences")
        try:
            result = cls(
                check_id=data["check_id"], intent=VerificationIntent(data["intent"]),
                project_type=data["project_type"], display_name=data["display_name"],
                argv=tuple(data["argv"]), cwd=data["cwd"],
                relevant_paths=tuple(data["relevant_paths"]), required=data["required"],
                available=data["available"],
                timeout_seconds=data["timeout_seconds"], output_limit=data["output_limit"],
                discovery_reason=data["discovery_reason"],
            )
            result.validate()
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Invalid verification check: {exc}") from exc
        return result


@dataclass
class VerificationPlan:
    project_type: str
    project_roots: tuple[str, ...]
    checks: list[VerificationCheck]
    unsupported_intents: list[VerificationIntent] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    run_id: str = field(default_factory=lambda: uuid4().hex)
    created_at: str = field(default_factory=_now)
    source_fingerprints: dict[str, str] = field(default_factory=dict)
    requirements_fingerprint: str | None = None

    def validate(self) -> None:
        _text(self.project_type, "verification project type", limit=64)
        _timestamp(self.created_at, "verification plan timestamp")
        _text(self.run_id, "verification run ID", limit=128)
        if (
            not isinstance(self.project_roots, tuple) or len(self.project_roots) > 32
            or any(not _safe_verification_path(path, allow_root=True) for path in self.project_roots)
            or len(set(self.project_roots)) != len(self.project_roots)
        ):
            raise ValueError("Invalid verification project roots")
        if not isinstance(self.checks, list) or len(self.checks) > 64:
            raise ValueError("Invalid verification checks")
        if any(not isinstance(check, VerificationCheck) for check in self.checks):
            raise ValueError("Invalid verification check")
        for check in self.checks:
            check.validate()
        if len({check.check_id for check in self.checks}) != len(self.checks):
            raise ValueError("Duplicate verification check ID")
        if (
            not isinstance(self.unsupported_intents, list) or len(self.unsupported_intents) > 8
            or any(not isinstance(intent, VerificationIntent) for intent in self.unsupported_intents)
        ):
            raise ValueError("Invalid unsupported verification intents")
        if not isinstance(self.warnings, list) or len(self.warnings) > 64:
            raise ValueError("Invalid verification warnings")
        for warning in self.warnings:
            _text(warning, "verification warning", limit=1024)
        if not isinstance(self.source_fingerprints, dict) or len(self.source_fingerprints) > 128:
            raise ValueError("Invalid verification source fingerprints")
        for path, fingerprint in self.source_fingerprints.items():
            if not _safe_verification_path(path) or not isinstance(fingerprint, str):
                raise ValueError("Invalid verification source fingerprint entry")
            if fingerprint != "missing" and not re.fullmatch(r"[0-9a-f]{64}", fingerprint):
                raise ValueError("Invalid verification source fingerprint")
        _text(
            self.requirements_fingerprint,
            "verification requirements fingerprint",
            optional=True,
            limit=64,
        )
        if self.requirements_fingerprint is not None and not re.fullmatch(
            r"[0-9a-f]{64}", self.requirements_fingerprint,
        ):
            raise ValueError("Invalid verification requirements fingerprint")

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return {
            "project_type": self.project_type, "project_roots": list(self.project_roots),
            "checks": [check.to_dict() for check in self.checks],
            "unsupported_intents": [intent.value for intent in self.unsupported_intents],
            "warnings": list(self.warnings), "run_id": self.run_id, "created_at": self.created_at,
            "source_fingerprints": dict(self.source_fingerprints),
            "requirements_fingerprint": self.requirements_fingerprint,
        }

    @classmethod
    def from_dict(cls, value: object) -> VerificationPlan:
        legacy_keys = {
            "project_type", "project_roots", "checks", "unsupported_intents",
            "warnings", "run_id", "created_at",
        }
        data = value
        valid_keys = {
            frozenset(legacy_keys),
            frozenset(legacy_keys | {"source_fingerprints"}),
            frozenset(legacy_keys | {"requirements_fingerprint"}),
            frozenset(legacy_keys | {"source_fingerprints", "requirements_fingerprint"}),
        }
        if not isinstance(data, dict) or frozenset(data) not in valid_keys:
            raise ValueError("Invalid agent verification plan fields")
        if any(not isinstance(data[key], list) for key in (
            "project_roots", "checks", "unsupported_intents", "warnings",
        )):
            raise ValueError("Invalid verification plan sequences")
        if not isinstance(data.get("source_fingerprints", {}), dict):
            raise ValueError("Invalid verification source fingerprints")
        try:
            result = cls(
                project_type=data["project_type"], project_roots=tuple(data["project_roots"]),
                checks=[VerificationCheck.from_dict(check) for check in data["checks"]],
                unsupported_intents=[
                    VerificationIntent(intent) for intent in data["unsupported_intents"]
                ],
                warnings=data["warnings"], run_id=data["run_id"], created_at=data["created_at"],
                source_fingerprints=dict(data.get("source_fingerprints", {})),
                requirements_fingerprint=data.get("requirements_fingerprint"),
            )
            result.validate()
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Invalid verification plan: {exc}") from exc
        return result


def _safe_verification_path(value: str, *, allow_root: bool = False) -> bool:
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        return False
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        return False
    path = PurePosixPath(value)
    return (
        not path.is_absolute() and path.as_posix() == value
        and (allow_root and value == "." or all(part not in {"", ".", ".."} for part in path.parts))
    )


TERMINAL_STATUSES = frozenset({
    AgentStatus.COMPLETED, AgentStatus.FAILED, AgentStatus.CANCELLED, AgentStatus.INTERRUPTED,
})

_ACTIVE_STATUSES = frozenset({
    AgentStatus.UNDERSTANDING, AgentStatus.PLANNING, AgentStatus.CONTEXT_GATHERING,
    AgentStatus.IMPLEMENTING, AgentStatus.VERIFYING, AgentStatus.REVIEWING,
    AgentStatus.REPAIRING, AgentStatus.WAITING_FOR_APPROVAL,
})

_TRANSITIONS: dict[AgentStatus, frozenset[AgentStatus]] = {
    AgentStatus.IDLE: frozenset({
        AgentStatus.UNDERSTANDING, AgentStatus.FAILED, AgentStatus.CANCELLED,
        AgentStatus.INTERRUPTED,
    }),
    AgentStatus.UNDERSTANDING: frozenset({AgentStatus.CONTEXT_GATHERING}),
    AgentStatus.CONTEXT_GATHERING: frozenset({AgentStatus.PLANNING}),
    AgentStatus.PLANNING: frozenset({
        AgentStatus.IMPLEMENTING, AgentStatus.WAITING_FOR_APPROVAL,
    }),
    AgentStatus.IMPLEMENTING: frozenset({
        AgentStatus.VERIFYING, AgentStatus.WAITING_FOR_APPROVAL, AgentStatus.COMPLETED,
    }),
    AgentStatus.VERIFYING: frozenset({AgentStatus.REPAIRING, AgentStatus.REVIEWING}),
    AgentStatus.REVIEWING: frozenset({AgentStatus.REPAIRING, AgentStatus.COMPLETED}),
    AgentStatus.REPAIRING: frozenset({AgentStatus.VERIFYING}),
    AgentStatus.WAITING_FOR_APPROVAL: frozenset(),
    AgentStatus.COMPLETED: frozenset(),
    AgentStatus.FAILED: frozenset(),
    AgentStatus.CANCELLED: frozenset(),
    AgentStatus.INTERRUPTED: frozenset(),
}


@dataclass
class AgentStep:
    step_id: str
    description: str
    verification: str | None = None
    status: StepStatus = StepStatus.PENDING
    purpose: str = ""
    depends_on: list[str] = field(default_factory=list)
    paths: list[str] = field(default_factory=list)
    symbols: list[str] = field(default_factory=list)
    operations: list[PlanOperation] = field(default_factory=list)
    expected_outcome: str = ""
    verification_criteria: list[str] = field(default_factory=list)
    verification_intents: list[VerificationIntent] = field(default_factory=list)
    required_outputs: list[str] = field(default_factory=list)

    def transition(self, status: StepStatus) -> None:
        if not isinstance(self.status, StepStatus) or not isinstance(status, StepStatus):
            raise ValueError("Invalid agent step status")
        allowed = {
            StepStatus.PENDING: {
                StepStatus.RUNNING, StepStatus.SKIPPED, StepStatus.BLOCKED,
                StepStatus.CANCELLED, StepStatus.INTERRUPTED,
            },
            StepStatus.RUNNING: {
                StepStatus.COMPLETED, StepStatus.FAILED, StepStatus.SKIPPED,
                StepStatus.BLOCKED, StepStatus.CANCELLED, StepStatus.INTERRUPTED,
            },
            StepStatus.COMPLETED: set(),
            StepStatus.FAILED: set(),
            StepStatus.SKIPPED: set(),
        }
        if not isinstance(status, StepStatus) or status not in allowed[self.status]:
            raise ValueError(f"Invalid agent step transition: {self.status} -> {status}")
        self.status = status

    def validate(self) -> None:
        _text(self.step_id, "step ID", limit=128)
        if not re.fullmatch(r"step-[a-z0-9]+(?:-[a-z0-9]+)*", self.step_id):
            raise ValueError("Invalid agent step ID")
        _text(self.description, "step description")
        _text(self.verification, "step verification", optional=True)
        if not isinstance(self.status, StepStatus):
            raise ValueError("Invalid agent step status")
        if not isinstance(self.purpose, str) or len(self.purpose) > 2048:
            raise ValueError("Invalid agent step purpose")
        if not isinstance(self.expected_outcome, str) or len(self.expected_outcome) > 2048:
            raise ValueError("Invalid agent step expected outcome")
        for values, label, limit, max_items in (
            (self.depends_on, "step dependencies", 128, 32),
            (self.paths, "step paths", 512, 32),
            (self.symbols, "step symbols", 512, 32),
            (self.verification_criteria, "step verification criteria", 1024, 16),
        ):
            if not isinstance(values, list) or len(values) > max_items:
                raise ValueError(f"Invalid agent {label}")
            for value in values:
                _text(value, label, limit=limit)
        if any(len(set(values)) != len(values) for values in (
            self.depends_on, self.paths, self.symbols, self.verification_criteria,
        )):
            raise ValueError("Duplicate agent step metadata")
        for path in self.paths:
            normalized = PurePosixPath(path)
            if (
                normalized.is_absolute() or normalized.as_posix() != path
                or any(part in {"", ".", ".."} for part in normalized.parts)
                or "\\" in path or "\x00" in path or ":" in normalized.parts[0]
            ):
                raise ValueError("Invalid agent plan path")
        if not isinstance(self.operations, list) or any(
            not isinstance(value, PlanOperation) for value in self.operations
        ):
            raise ValueError("Invalid agent step operations")
        if len(set(self.operations)) != len(self.operations):
            raise ValueError("Duplicate agent step operation")
        if (
            not isinstance(self.required_outputs, list)
            or len(self.required_outputs) > 32
            or any(not _safe_verification_path(path) for path in self.required_outputs)
            or len(set(self.required_outputs)) != len(self.required_outputs)
            or any(path not in self.paths for path in self.required_outputs)
        ):
            raise ValueError("Invalid required agent step outputs")
        if self.required_outputs and not set(self.operations) & {
            PlanOperation.CREATE, PlanOperation.MODIFY, PlanOperation.DOCUMENT,
        }:
            raise ValueError("Required outputs need a create, modify, or document operation")
        if not isinstance(self.verification_intents, list) or any(
            not isinstance(value, VerificationIntent) for value in self.verification_intents
        ):
            raise ValueError("Invalid agent step verification intents")
        if len(set(self.verification_intents)) != len(self.verification_intents):
            raise ValueError("Duplicate agent step verification intent")

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        result = asdict(self)
        result["operations"] = [value.value for value in self.operations]
        result["verification_intents"] = [value.value for value in self.verification_intents]
        return result

    @classmethod
    def from_dict(cls, value: object) -> AgentStep:
        legacy_keys = {"step_id", "description", "verification", "status"}
        plan_keys = legacy_keys | {
            "purpose", "depends_on", "paths", "symbols", "operations", "expected_outcome",
            "verification_criteria", "verification_intents",
        }
        required_output_keys = plan_keys | {"required_outputs"}
        if not isinstance(value, dict) or set(value) not in (
            legacy_keys, plan_keys, required_output_keys,
        ):
            raise ValueError("Invalid agent step fields")
        data = value
        try:
            result = cls(
                data["step_id"], data["description"], data["verification"],
                StepStatus(data["status"]),
                data.get("purpose", ""),
                data.get("depends_on", []),
                data.get("paths", []),
                data.get("symbols", []),
                [PlanOperation(operation) for operation in data.get("operations", [])],
                data.get("expected_outcome", ""),
                data.get("verification_criteria", []),
                [
                    VerificationIntent(intent)
                    for intent in data.get("verification_intents", [])
                ],
                data.get("required_outputs", []),
            )
            result.validate()
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Invalid agent step: {exc}") from exc
        return result


@dataclass
class AgentPlan:
    goal: str
    steps: list[AgentStep]
    plan_id: str = field(default_factory=lambda: uuid4().hex)
    created_at: str = field(default_factory=_now)
    assumptions: list[str] = field(default_factory=list)
    uncertainties: list[str] = field(default_factory=list)
    completion_criteria: list[str] = field(default_factory=list)
    verification_intent: list[VerificationIntent] = field(default_factory=list)
    executable_order: list[str] = field(default_factory=list)
    schema_version: int = 2
    context_hash: str | None = None
    context_truncated: bool = False
    planner_provider: str | None = None
    planner_model: str | None = None
    planning_attempts: int = 0
    validation_warnings: list[str] = field(default_factory=list)

    def validate(self) -> None:
        _text(self.plan_id, "plan ID", limit=128)
        _text(self.goal, "plan goal")
        _timestamp(self.created_at, "plan timestamp")
        if type(self.schema_version) is not int or self.schema_version != 2:
            raise ValueError("Unsupported agent plan schema version")
        if not isinstance(self.steps, list) or not 1 <= len(self.steps) <= 256:
            raise ValueError("Invalid agent plan steps")
        for step in self.steps:
            if not isinstance(step, AgentStep):
                raise ValueError("Invalid agent plan step")
            step.validate()
        if len({step.step_id for step in self.steps}) != len(self.steps):
            raise ValueError("Duplicate agent step ID")
        step_ids = {step.step_id for step in self.steps}
        dependencies = {step.step_id: set(step.depends_on) for step in self.steps}
        if any(
            dependency not in step_ids or dependency == step_id
            for step_id, values in dependencies.items() for dependency in values
        ):
            raise ValueError("Invalid agent plan step dependency")
        unresolved = {step_id: set(values) for step_id, values in dependencies.items()}
        while unresolved:
            ready = [step_id for step_id, values in unresolved.items() if not values]
            if not ready:
                raise ValueError("Agent plan dependencies contain a cycle")
            completed = set(ready)
            for step_id in completed:
                del unresolved[step_id]
            for values in unresolved.values():
                values.difference_update(completed)
        for values, label, limit, max_items in (
            (self.assumptions, "plan assumptions", 2048, 32),
            (self.uncertainties, "plan uncertainties", 2048, 32),
            (self.completion_criteria, "plan completion criteria", 1024, 32),
            (self.validation_warnings, "plan validation warnings", 2048, 64),
            (self.executable_order, "plan executable order", 128, 256),
        ):
            if not isinstance(values, list) or len(values) > max_items:
                raise ValueError(f"Invalid agent {label}")
            for value in values:
                _text(value, label, limit=limit)
        if not isinstance(self.verification_intent, list) or any(
            not isinstance(value, VerificationIntent) for value in self.verification_intent
        ):
            raise ValueError("Invalid agent plan verification intent")
        if len(self.verification_intent) > 8 or len(set(self.verification_intent)) != len(self.verification_intent):
            raise ValueError("Invalid or duplicate agent plan verification intent")
        if self.executable_order and (
            len(self.executable_order) != len(self.steps)
            or set(self.executable_order) != {step.step_id for step in self.steps}
        ):
            raise ValueError("Invalid agent plan executable order")
        if self.executable_order:
            positions = {step_id: index for index, step_id in enumerate(self.executable_order)}
            if any(
                positions[dependency] >= positions[step.step_id]
                for step in self.steps for dependency in step.depends_on
            ):
                raise ValueError("Agent plan executable order violates dependencies")
        _text(self.context_hash, "plan context hash", optional=True, limit=64)
        if self.context_hash is not None and not re.fullmatch(r"[0-9a-f]{64}", self.context_hash):
            raise ValueError("Invalid agent plan context hash")
        if type(self.context_truncated) is not bool:
            raise ValueError("Invalid agent plan context truncation state")
        _text(self.planner_provider, "planner provider", optional=True, limit=128)
        _text(self.planner_model, "planner model", optional=True, limit=512)
        if type(self.planning_attempts) is not int or not 0 <= self.planning_attempts <= 8:
            raise ValueError("Invalid agent planning attempt count")

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return {
            "plan_id": self.plan_id, "goal": self.goal, "created_at": self.created_at,
            "steps": [step.to_dict() for step in self.steps],
            "assumptions": list(self.assumptions),
            "uncertainties": list(self.uncertainties),
            "completion_criteria": list(self.completion_criteria),
            "verification_intent": [value.value for value in self.verification_intent],
            "executable_order": list(self.executable_order),
            "schema_version": self.schema_version,
            "context_hash": self.context_hash,
            "context_truncated": self.context_truncated,
            "planner_provider": self.planner_provider,
            "planner_model": self.planner_model,
            "planning_attempts": self.planning_attempts,
            "validation_warnings": list(self.validation_warnings),
        }

    @classmethod
    def from_dict(cls, value: object) -> AgentPlan:
        legacy_keys = {"plan_id", "goal", "created_at", "steps"}
        plan_keys = legacy_keys | {
            "assumptions", "uncertainties", "completion_criteria", "verification_intent",
            "executable_order", "schema_version", "context_hash", "context_truncated",
            "planner_provider", "planner_model", "planning_attempts", "validation_warnings",
        }
        if not isinstance(value, dict) or set(value) not in (legacy_keys, plan_keys):
            raise ValueError("Invalid agent plan fields")
        data = value
        if not isinstance(data["steps"], list):
            raise ValueError("Invalid agent plan steps")
        try:
            result = cls(
                data["goal"], [AgentStep.from_dict(step) for step in data["steps"]],
                data["plan_id"], data["created_at"],
                data.get("assumptions", []),
                data.get("uncertainties", []),
                data.get("completion_criteria", []),
                [
                    VerificationIntent(intent)
                    for intent in data.get("verification_intent", [])
                ],
                data.get("executable_order", []),
                data.get("schema_version", 2),
                data.get("context_hash"),
                data.get("context_truncated", False),
                data.get("planner_provider"),
                data.get("planner_model"),
                data.get("planning_attempts", 0),
                data.get("validation_warnings", []),
            )
            result.validate()
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Invalid agent plan: {exc}") from exc
        return result


@dataclass
class AgentExecution:
    step_id: str
    tool_name: str
    status: ExecutionStatus = ExecutionStatus.PENDING
    execution_id: str = field(default_factory=lambda: uuid4().hex)
    started_at: str = field(default_factory=_now)
    completed_at: str | None = None
    result_summary: str | None = None
    error_type: AgentErrorType | None = None
    operation: PlanOperation | None = None
    target_path: str | None = None
    approval_state: ApprovalStatus = ApprovalStatus.NOT_REQUIRED
    output_truncated: bool = False

    def validate(self) -> None:
        _text(self.execution_id, "execution ID", limit=128)
        _text(self.step_id, "execution step ID", limit=128)
        _text(self.tool_name, "execution tool name", limit=128)
        _timestamp(self.started_at, "execution start timestamp")
        if self.completed_at is not None:
            _timestamp(self.completed_at, "execution completion timestamp")
        _text(self.result_summary, "execution result summary", optional=True, limit=4096)
        if self.error_type is not None and not isinstance(self.error_type, AgentErrorType):
            raise ValueError("Invalid agent execution error type")
        if not isinstance(self.status, ExecutionStatus):
            raise ValueError("Invalid agent execution status")
        if self.operation is not None and not isinstance(self.operation, PlanOperation):
            raise ValueError("Invalid agent execution operation")
        if self.target_path is not None:
            _text(self.target_path, "execution target path", limit=512)
            path = PurePosixPath(self.target_path)
            if (
                path.is_absolute() or path.as_posix() != self.target_path
                or any(part in {"", ".", ".."} for part in path.parts)
                or "\\" in self.target_path or "\x00" in self.target_path
            ):
                raise ValueError("Invalid agent execution target path")
        if not isinstance(self.approval_state, ApprovalStatus):
            raise ValueError("Invalid agent execution approval state")
        if type(self.output_truncated) is not bool:
            raise ValueError("Invalid agent execution output truncation state")

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        result = asdict(self)
        result["status"] = self.status.value
        result["error_type"] = self.error_type.value if self.error_type is not None else None
        result["operation"] = self.operation.value if self.operation is not None else None
        result["approval_state"] = self.approval_state.value
        return result

    @classmethod
    def from_dict(cls, value: object) -> AgentExecution:
        legacy_keys = {
            "execution_id", "step_id", "tool_name", "status", "started_at",
            "completed_at", "result_summary", "error_type",
        }
        keys = legacy_keys | {"operation", "target_path", "approval_state", "output_truncated"}
        if not isinstance(value, dict) or set(value) not in {frozenset(legacy_keys), frozenset(keys)}:
            raise ValueError("Invalid agent execution fields")
        data = value
        try:
            result = cls(
                step_id=data["step_id"], tool_name=data["tool_name"],
                status=ExecutionStatus(data["status"]), execution_id=data["execution_id"],
                started_at=data["started_at"], completed_at=data["completed_at"],
                result_summary=data["result_summary"],
                error_type=AgentErrorType(data["error_type"]) if data["error_type"] is not None else None,
                operation=(
                    PlanOperation(data["operation"]) if data.get("operation") is not None else None
                ),
                target_path=data.get("target_path"),
                approval_state=ApprovalStatus(data.get(
                    "approval_state", ApprovalStatus.NOT_REQUIRED.value,
                )),
                output_truncated=data.get("output_truncated", False),
            )
            result.validate()
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Invalid agent execution: {exc}") from exc
        return result


@dataclass
class VerificationResult:
    command: str
    status: VerificationStatus
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    duration: float = 0.0
    timed_out: bool = False
    truncated: bool = False
    check_id: str | None = None
    intent: VerificationIntent | None = None
    verifier: str | None = None
    cwd: str = "."
    required: bool = True
    approval_state: ApprovalStatus = ApprovalStatus.NOT_REQUIRED
    relevant_paths: tuple[str, ...] = ()
    failure_summary: str | None = None
    infrastructure_error: str | None = None
    repairability: Repairability = Repairability.UNKNOWN
    execution_id: str | None = None
    run_id: str | None = None

    def validate(self) -> None:
        _text(self.command, "verification command", limit=4096)
        if not isinstance(self.status, VerificationStatus):
            raise ValueError("Invalid verification status")
        if self.exit_code is not None and (type(self.exit_code) is not int):
            raise ValueError("Invalid verification exit code")
        if not isinstance(self.stdout, str) or len(self.stdout.encode("utf-8")) > 65_536:
            raise ValueError("Invalid verification stdout")
        if not isinstance(self.stderr, str) or len(self.stderr.encode("utf-8")) > 65_536:
            raise ValueError("Invalid verification stderr")
        if isinstance(self.duration, bool) or not isinstance(self.duration, (int, float)):
            raise ValueError("Invalid verification duration")
        try:
            if not math.isfinite(self.duration) or self.duration < 0:
                raise ValueError("Invalid verification duration")
        except OverflowError as exc:
            raise ValueError("Invalid verification duration") from exc
        if type(self.timed_out) is not bool or type(self.truncated) is not bool:
            raise ValueError("Invalid verification flags")
        _text(self.check_id, "verification result check ID", optional=True, limit=128)
        if self.intent is not None and not isinstance(self.intent, VerificationIntent):
            raise ValueError("Invalid verification result intent")
        _text(self.verifier, "verification result verifier", optional=True, limit=64)
        if not _safe_verification_path(self.cwd, allow_root=True):
            raise ValueError("Invalid verification result working directory")
        if type(self.required) is not bool:
            raise ValueError("Invalid verification result required flag")
        if not isinstance(self.approval_state, ApprovalStatus):
            raise ValueError("Invalid verification result approval state")
        if (
            not isinstance(self.relevant_paths, tuple) or len(self.relevant_paths) > 64
            or any(not _safe_verification_path(path) for path in self.relevant_paths)
        ):
            raise ValueError("Invalid verification result relevant paths")
        _text(self.failure_summary, "verification failure summary", optional=True, limit=4096)
        _text(self.infrastructure_error, "verification infrastructure error", optional=True, limit=2048)
        if not isinstance(self.repairability, Repairability):
            raise ValueError("Invalid verification repairability")
        _text(self.execution_id, "verification execution ID", optional=True, limit=128)
        _text(self.run_id, "verification run ID", optional=True, limit=128)

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        result = asdict(self)
        result["status"] = self.status.value
        result["intent"] = self.intent.value if self.intent is not None else None
        result["approval_state"] = self.approval_state.value
        result["repairability"] = self.repairability.value
        result["relevant_paths"] = list(self.relevant_paths)
        return result

    @classmethod
    def from_dict(cls, value: object) -> VerificationResult:
        legacy_keys = {
            "command", "status", "exit_code", "stdout", "stderr", "duration", "timed_out", "truncated",
        }
        extended_keys = legacy_keys | {
            "check_id", "intent", "verifier", "cwd", "required", "approval_state",
            "relevant_paths", "failure_summary", "infrastructure_error", "repairability",
            "execution_id", "run_id",
        }
        previous_extended_keys = extended_keys - {"run_id"}
        if not isinstance(value, dict) or set(value) not in {
            frozenset(legacy_keys), frozenset(previous_extended_keys), frozenset(extended_keys),
        }:
            raise ValueError("Invalid agent verification fields")
        data = value
        if "relevant_paths" in data and not isinstance(data["relevant_paths"], list):
            raise ValueError("Invalid verification result paths")
        try:
            result = cls(
                command=data["command"], status=VerificationStatus(data["status"]),
                exit_code=data["exit_code"], stdout=data["stdout"], stderr=data["stderr"],
                duration=data["duration"], timed_out=data["timed_out"], truncated=data["truncated"],
                check_id=data.get("check_id"),
                intent=VerificationIntent(data["intent"]) if data.get("intent") is not None else None,
                verifier=data.get("verifier"), cwd=data.get("cwd", "."),
                required=data.get("required", True),
                approval_state=ApprovalStatus(data.get(
                    "approval_state", ApprovalStatus.NOT_REQUIRED.value,
                )),
                relevant_paths=tuple(data.get("relevant_paths", [])),
                failure_summary=data.get("failure_summary"),
                infrastructure_error=data.get("infrastructure_error"),
                repairability=Repairability(data.get("repairability", Repairability.UNKNOWN.value)),
                execution_id=data.get("execution_id"),
                run_id=data.get("run_id"),
            )
            result.validate()
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Invalid agent verification result: {exc}") from exc
        return result


@dataclass
class RepairAttempt:
    attempt: int
    diagnosis: str
    status: RepairStatus = RepairStatus.PENDING
    step_id: str | None = None
    verification_index: int | None = None
    triggering_run_id: str | None = None
    triggering_check_id: str | None = None
    intended_targets: tuple[str, ...] = ()
    mutated_paths: tuple[str, ...] = ()
    execution_ids: tuple[str, ...] = ()
    provider: str | None = None
    model: str | None = None
    started_at: str = field(default_factory=_now)
    completed_at: str | None = None
    error: str | None = None
    next_verification_run_id: str | None = None
    repeated_failure: bool = False
    no_progress: bool = False

    def validate(self) -> None:
        if type(self.attempt) is not int or self.attempt < 1:
            raise ValueError("Invalid repair attempt number")
        _text(self.diagnosis, "repair diagnosis")
        _text(self.step_id, "repair step ID", optional=True, limit=128)
        _text(self.triggering_run_id, "repair triggering run ID", optional=True, limit=128)
        _text(self.triggering_check_id, "repair triggering check ID", optional=True, limit=128)
        _text(self.provider, "repair provider", optional=True, limit=128)
        _text(self.model, "repair model", optional=True, limit=512)
        _timestamp(self.started_at, "repair attempt start timestamp")
        if self.completed_at is not None:
            _timestamp(self.completed_at, "repair attempt completion timestamp")
        _text(self.error, "repair attempt error", optional=True, limit=2048)
        _text(self.next_verification_run_id, "repair verification run ID", optional=True, limit=128)
        if self.verification_index is not None and (
            type(self.verification_index) is not int or self.verification_index < 0
        ):
            raise ValueError("Invalid repair verification index")
        if not isinstance(self.status, RepairStatus):
            raise ValueError("Invalid repair status")
        for values, label, max_items in (
            (self.intended_targets, "intended repair targets", 32),
            (self.mutated_paths, "mutated repair paths", 32),
            (self.execution_ids, "repair execution IDs", 64),
        ):
            if not isinstance(values, tuple) or len(values) > max_items:
                raise ValueError(f"Invalid {label}")
            if len(set(values)) != len(values):
                raise ValueError(f"Duplicate {label}")
            for value in values:
                _text(value, label, limit=512)
                if label != "repair execution IDs":
                    path = PurePosixPath(value)
                    if (
                        path.is_absolute() or path.as_posix() != value
                        or any(part in {"", ".", ".."} for part in path.parts)
                        or "\\" in value or "\x00" in value
                    ):
                        raise ValueError(f"Invalid {label}")
        if type(self.repeated_failure) is not bool or type(self.no_progress) is not bool:
            raise ValueError("Invalid repair attempt flags")

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        result = asdict(self)
        for key in ("intended_targets", "mutated_paths", "execution_ids"):
            result[key] = list(result[key])
        return result

    @classmethod
    def from_dict(cls, value: object) -> RepairAttempt:
        legacy_keys = {
            "attempt", "diagnosis", "status", "step_id", "verification_index",
        }
        extended_keys = legacy_keys | {
            "triggering_run_id", "triggering_check_id", "intended_targets",
            "mutated_paths", "execution_ids", "provider", "model", "started_at",
            "completed_at", "error", "next_verification_run_id", "repeated_failure",
            "no_progress",
        }
        if not isinstance(value, dict) or set(value) not in {
            frozenset(legacy_keys), frozenset(extended_keys),
        }:
            raise ValueError("Invalid agent repair attempt fields")
        data = value
        try:
            result = cls(
                attempt=data["attempt"], diagnosis=data["diagnosis"],
                status=RepairStatus(data["status"]), step_id=data["step_id"],
                verification_index=data["verification_index"],
                triggering_run_id=data.get("triggering_run_id"),
                triggering_check_id=data.get("triggering_check_id"),
                intended_targets=tuple(data.get("intended_targets", [])),
                mutated_paths=tuple(data.get("mutated_paths", [])),
                execution_ids=tuple(data.get("execution_ids", [])),
                provider=data.get("provider"),
                model=data.get("model"),
                started_at=data.get("started_at", _now()),
                completed_at=data.get("completed_at"),
                error=data.get("error"),
                next_verification_run_id=data.get("next_verification_run_id"),
                repeated_failure=data.get("repeated_failure", False),
                no_progress=data.get("no_progress", False),
            )
            result.validate()
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Invalid agent repair attempt: {exc}") from exc
        return result


@dataclass
class ReviewFinding:
    finding_id: str
    category: ReviewCategory
    severity: ReviewSeverity
    confidence: ReviewConfidence
    description: str
    evidence: str
    impact: str
    recommendation: str
    blocking: bool
    path: str | None = None
    symbol: str | None = None
    start_line: int | None = None
    end_line: int | None = None
    plan_step_id: str | None = None
    execution_id: str | None = None

    def validate(self) -> None:
        _text(self.finding_id, "review finding ID", limit=128)
        if not re.fullmatch(r"finding-[a-z0-9-]{1,120}", self.finding_id):
            raise ValueError("Invalid review finding ID")
        for value, label, limit in (
            (self.description, "review finding description", 2048),
            (self.evidence, "review finding evidence", 4096),
            (self.impact, "review finding impact", 2048),
            (self.recommendation, "review finding recommendation", 2048),
        ):
            _text(value, label, limit=limit)
        if not isinstance(self.category, ReviewCategory):
            raise ValueError("Invalid review finding category")
        if not isinstance(self.severity, ReviewSeverity):
            raise ValueError("Invalid review finding severity")
        if not isinstance(self.confidence, ReviewConfidence):
            raise ValueError("Invalid review finding confidence")
        if type(self.blocking) is not bool:
            raise ValueError("Invalid review finding blocking state")
        if self.path is not None and not _safe_verification_path(self.path):
            raise ValueError("Invalid review finding path")
        _text(self.symbol, "review finding symbol", optional=True, limit=512)
        for line in (self.start_line, self.end_line):
            if line is not None and (type(line) is not int or line < 1):
                raise ValueError("Invalid review finding line")
        if (
            self.start_line is not None and self.end_line is not None
            and self.end_line < self.start_line
        ):
            raise ValueError("Invalid review finding line range")
        _text(self.plan_step_id, "review finding plan step", optional=True, limit=128)
        _text(self.execution_id, "review finding execution ID", optional=True, limit=128)

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        result = asdict(self)
        result["category"] = self.category.value
        result["severity"] = self.severity.value
        result["confidence"] = self.confidence.value
        return result

    @classmethod
    def from_dict(cls, value: object) -> ReviewFinding:
        keys = {
            "finding_id", "category", "severity", "confidence", "description", "evidence",
            "impact", "recommendation", "blocking", "path", "symbol", "start_line",
            "end_line", "plan_step_id", "execution_id",
        }
        data = _object(value, "review finding", keys)
        try:
            result = cls(
                finding_id=data["finding_id"],
                category=ReviewCategory(data["category"]),
                severity=ReviewSeverity(data["severity"]),
                confidence=ReviewConfidence(data["confidence"]),
                description=data["description"],
                evidence=data["evidence"],
                impact=data["impact"],
                recommendation=data["recommendation"],
                blocking=data["blocking"],
                path=data["path"],
                symbol=data["symbol"],
                start_line=data["start_line"],
                end_line=data["end_line"],
                plan_step_id=data["plan_step_id"],
                execution_id=data["execution_id"],
            )
            result.validate()
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Invalid agent review finding: {exc}") from exc
        return result


@dataclass
class ReviewRecord:
    task_id: str
    plan_id: str | None
    verification_run_id: str | None
    outcome: ReviewOutcome
    provider: str | None
    model: str | None
    workspace_identity: str | None
    backend_identity: str | None
    context_fingerprint: str
    findings: list[ReviewFinding]
    summary: str
    started_at: str = field(default_factory=_now)
    completed_at: str = field(default_factory=_now)
    limitations: list[str] = field(default_factory=list)

    def validate(self) -> None:
        for value, label, limit in (
            (self.task_id, "review task ID", 128),
            (self.summary, "review summary", 4096),
        ):
            _text(value, label, limit=limit)
        for value, label, limit in (
            (self.plan_id, "review plan ID", 128),
            (self.verification_run_id, "review verification run ID", 128),
            (self.provider, "review provider", 128),
            (self.model, "review model", 512),
            (self.workspace_identity, "review workspace identity", 2048),
            (self.backend_identity, "review backend identity", 128),
        ):
            _text(value, label, optional=True, limit=limit)
        if not isinstance(self.outcome, ReviewOutcome):
            raise ValueError("Invalid review outcome")
        if not isinstance(self.context_fingerprint, str) or not re.fullmatch(
            r"[0-9a-f]{64}", self.context_fingerprint,
        ):
            raise ValueError("Invalid review context fingerprint")
        _timestamp(self.started_at, "review start timestamp")
        _timestamp(self.completed_at, "review completion timestamp")
        if not isinstance(self.findings, list) or len(self.findings) > 64:
            raise ValueError("Invalid review findings")
        for finding in self.findings:
            if not isinstance(finding, ReviewFinding):
                raise ValueError("Invalid review finding")
            finding.validate()
        if len({finding.finding_id for finding in self.findings}) != len(self.findings):
            raise ValueError("Duplicate review finding ID")
        if not isinstance(self.limitations, list) or len(self.limitations) > 32:
            raise ValueError("Invalid review limitations")
        for limitation in self.limitations:
            _text(limitation, "review limitation", limit=1024)
        if len(json.dumps(self.to_dict(), ensure_ascii=True).encode("utf-8")) > 128 * 1024:
            raise ValueError("Review record exceeds 128 KiB")

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "plan_id": self.plan_id,
            "verification_run_id": self.verification_run_id,
            "outcome": self.outcome.value,
            "provider": self.provider,
            "model": self.model,
            "workspace_identity": self.workspace_identity,
            "backend_identity": self.backend_identity,
            "context_fingerprint": self.context_fingerprint,
            "findings": [item.to_dict() for item in self.findings],
            "summary": self.summary,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "limitations": list(self.limitations),
        }

    @classmethod
    def from_dict(cls, value: object) -> ReviewRecord:
        keys = {
            "task_id", "plan_id", "verification_run_id", "outcome", "provider", "model",
            "workspace_identity", "backend_identity", "context_fingerprint", "findings",
            "summary", "started_at", "completed_at", "limitations",
        }
        data = _object(value, "review record", keys)
        if not isinstance(data["findings"], list) or not isinstance(data["limitations"], list):
            raise ValueError("Invalid agent review sequences")
        try:
            result = cls(
                task_id=data["task_id"],
                plan_id=data["plan_id"],
                verification_run_id=data["verification_run_id"],
                outcome=ReviewOutcome(data["outcome"]),
                provider=data["provider"],
                model=data["model"],
                workspace_identity=data["workspace_identity"],
                backend_identity=data["backend_identity"],
                context_fingerprint=data["context_fingerprint"],
                findings=[ReviewFinding.from_dict(item) for item in data["findings"]],
                summary=data["summary"],
                started_at=data["started_at"],
                completed_at=data["completed_at"],
                limitations=data["limitations"],
            )
            result.validate()
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Invalid agent review record: {exc}") from exc
        return result


@dataclass
class AgentTask:
    goal: str
    task_id: str = field(default_factory=lambda: uuid4().hex)
    status: AgentStatus = AgentStatus.IDLE
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)
    selected_model: str | None = None
    plan: AgentPlan | None = None
    current_step_id: str | None = None
    executions: list[AgentExecution] = field(default_factory=list)
    verification_results: list[VerificationResult] = field(default_factory=list)
    verification_plan: VerificationPlan | None = None
    verification_outcome: VerificationOutcome | None = None
    current_verification_check_id: str | None = None
    repair_attempts: list[RepairAttempt] = field(default_factory=list)
    repair_outcome: RepairOutcome | None = None
    review_record: ReviewRecord | None = None
    terminal_summary: str | None = None
    approval_resume_state: AgentStatus | None = None

    def transition(self, status: AgentStatus) -> None:
        if not isinstance(status, AgentStatus):
            raise ValueError("Invalid agent task status")
        self.validate()
        if self.status in TERMINAL_STATUSES:
            raise ValueError(f"Agent task is terminal: {self.status}")
        if status == AgentStatus.WAITING_FOR_APPROVAL:
            if self.status not in _ACTIVE_STATUSES or self.status == AgentStatus.WAITING_FOR_APPROVAL:
                raise ValueError("Approval can only pause an active agent task")
            self.approval_resume_state = self.status
        elif status in {
            AgentStatus.FAILED, AgentStatus.CANCELLED, AgentStatus.INTERRUPTED,
        }:
            self.approval_resume_state = None
            self._mark_pending_executions_interrupted()
        elif status not in _TRANSITIONS[self.status]:
            raise ValueError(f"Invalid agent task transition: {self.status} -> {status}")
        if status == AgentStatus.COMPLETED and any(
            execution.status == ExecutionStatus.PENDING for execution in self.executions
        ):
            raise ValueError("Cannot complete an agent task with an uncertain pending execution")
        if status not in {AgentStatus.WAITING_FOR_APPROVAL}:
            self.approval_resume_state = None
        self.status = status
        self.updated_at = _now()

    def resolve_approval(self, approved: bool) -> None:
        self.validate()
        if self.status != AgentStatus.WAITING_FOR_APPROVAL or self.approval_resume_state is None:
            raise ValueError("Agent task is not waiting for approval")
        if type(approved) is not bool:
            raise ValueError("Approval decision must be a boolean")
        resume = self.approval_resume_state
        self.approval_resume_state = None
        if approved:
            self.status = resume
        else:
            self._mark_pending_executions_interrupted()
            self._mark_pending_steps(StepStatus.BLOCKED)
            self.status = AgentStatus.FAILED
            self.terminal_summary = "Approval denied; no further task steps were executed."
        self.updated_at = _now()

    def rollback_review_completion(self, outcome: ReviewOutcome, summary: str) -> None:
        if (
            self.status != AgentStatus.COMPLETED
            or self.review_record is None
            or self.review_record.outcome not in {
                ReviewOutcome.PASSED, ReviewOutcome.PASSED_WITH_WARNINGS,
            }
            or outcome not in {ReviewOutcome.ERROR, ReviewOutcome.CANCELLED}
        ):
            raise ValueError("Only a just-completed review can be rolled back")
        _text(summary, "review rollback summary", limit=4096)
        self.status = (
            AgentStatus.CANCELLED
            if outcome == ReviewOutcome.CANCELLED
            else AgentStatus.REVIEWING
        )
        self.review_record.outcome = outcome
        self.review_record.summary = summary
        self.review_record.completed_at = _now()
        self.terminal_summary = summary
        self.updated_at = _now()

    def recover_interrupted(self) -> bool:
        repair_verification_pending = (
            self.status == AgentStatus.VERIFYING
            and self.repair_outcome == RepairOutcome.REPAIRED_PENDING_VERIFICATION
        )
        has_pending = any(execution.status == ExecutionStatus.PENDING for execution in self.executions)
        has_verification = any(
            result.status == VerificationStatus.RUNNING for result in self.verification_results
        )
        if self.status not in _ACTIVE_STATUSES and not has_pending and not has_verification:
            return False
        self._mark_pending_executions_interrupted()
        if self.plan is not None and self.current_step_id is not None:
            step = next(
                (step for step in self.plan.steps if step.step_id == self.current_step_id),
                None,
            )
            if step is not None and step.status == StepStatus.RUNNING:
                step.status = StepStatus.INTERRUPTED
        self._mark_pending_steps(StepStatus.BLOCKED)
        for result in self.verification_results:
            if result.status == VerificationStatus.RUNNING:
                result.status = VerificationStatus.INTERRUPTED
                result.infrastructure_error = "Verification was interrupted; command outcome is uncertain."
                result.repairability = Repairability.NOT_REPAIRABLE
        for attempt in self.repair_attempts:
            if attempt.status == RepairStatus.PENDING:
                attempt.status = RepairStatus.INTERRUPTED
                attempt.completed_at = _now()
                attempt.error = "Repair interrupted; pending mutations were not replayed."
        if repair_verification_pending or any(
            attempt.status == RepairStatus.INTERRUPTED
            for attempt in self.repair_attempts
        ):
            self.repair_outcome = RepairOutcome.REPAIR_INTERRUPTED
        if has_verification:
            self.verification_outcome = VerificationOutcome.UNKNOWN
            self.current_verification_check_id = None
        self.status = AgentStatus.INTERRUPTED
        self.approval_resume_state = None
        self.terminal_summary = "Task interrupted; uncertain operations were not replayed."
        self.updated_at = _now()
        self.validate()
        return True

    def _mark_pending_executions_interrupted(self) -> None:
        for execution in self.executions:
            if execution.status == ExecutionStatus.PENDING:
                execution.status = ExecutionStatus.INTERRUPTED
                execution.error_type = AgentErrorType.INTERRUPTED

    def _mark_pending_steps(self, status: StepStatus) -> None:
        if self.plan is None:
            return
        for step in self.plan.steps:
            if step.status == StepStatus.PENDING:
                step.status = status

    def validate(self) -> None:
        _text(self.task_id, "task ID", limit=128)
        _text(self.goal, "task goal")
        _timestamp(self.created_at, "task creation timestamp")
        _timestamp(self.updated_at, "task update timestamp")
        _text(self.selected_model, "selected model", optional=True, limit=512)
        _text(self.current_step_id, "current step ID", optional=True, limit=128)
        _text(self.terminal_summary, "terminal summary", optional=True, limit=4096)
        _text(self.current_verification_check_id, "current verification check ID", optional=True, limit=128)
        if not isinstance(self.status, AgentStatus):
            raise ValueError("Invalid agent task status")
        if self.approval_resume_state is not None and (
            self.status != AgentStatus.WAITING_FOR_APPROVAL
            or self.approval_resume_state not in _ACTIVE_STATUSES
            or self.approval_resume_state == AgentStatus.WAITING_FOR_APPROVAL
        ):
            raise ValueError("Invalid agent approval-resume state")
        if self.status == AgentStatus.WAITING_FOR_APPROVAL and self.approval_resume_state is None:
            raise ValueError("Waiting agent task has no approval-resume state")
        if self.plan is not None:
            if not isinstance(self.plan, AgentPlan):
                raise ValueError("Invalid agent task plan")
            self.plan.validate()
            step_ids = {step.step_id for step in self.plan.steps}
            if self.current_step_id is not None and self.current_step_id not in step_ids:
                raise ValueError("Current agent task step is not in its plan")
        elif self.current_step_id is not None:
            raise ValueError("Agent task step requires a plan")
        for items, label in (
            (self.executions, "executions"),
            (self.verification_results, "verification results"),
            (self.repair_attempts, "repair attempts"),
        ):
            if not isinstance(items, list) or len(items) > 512:
                raise ValueError(f"Invalid agent {label}")
        if self.verification_plan is not None:
            if not isinstance(self.verification_plan, VerificationPlan):
                raise ValueError("Invalid agent verification plan")
            self.verification_plan.validate()
        if self.verification_outcome is not None and not isinstance(
            self.verification_outcome, VerificationOutcome,
        ):
            raise ValueError("Invalid agent verification outcome")
        if self.repair_outcome is not None and not isinstance(self.repair_outcome, RepairOutcome):
            raise ValueError("Invalid agent repair outcome")
        if self.review_record is not None:
            if not isinstance(self.review_record, ReviewRecord):
                raise ValueError("Invalid agent review record")
            self.review_record.validate()
            if self.review_record.task_id != self.task_id:
                raise ValueError("Review record references a different task")
            if (
                self.review_record.plan_id is not None
                and (self.plan is None or self.review_record.plan_id != self.plan.plan_id)
            ):
                raise ValueError("Review record references a different plan")
            if (
                self.review_record.verification_run_id is not None
                and (
                    self.verification_plan is None
                    or self.review_record.verification_run_id != self.verification_plan.run_id
                )
            ):
                raise ValueError("Review record references a different verification run")
            step_ids = {step.step_id for step in self.plan.steps}
            execution_ids = {execution.execution_id for execution in self.executions}
            if any(
                finding.plan_step_id is not None and finding.plan_step_id not in step_ids
                or finding.execution_id is not None and finding.execution_id not in execution_ids
                for finding in self.review_record.findings
            ):
                raise ValueError("Review finding references unknown task evidence")
        if self.current_verification_check_id is not None and (
            self.verification_plan is None
            or self.current_verification_check_id not in {
                check.check_id for check in self.verification_plan.checks
            }
        ):
            raise ValueError("Current verification check is not in its plan")
        for item in self.executions:
            if not isinstance(item, AgentExecution):
                raise ValueError("Invalid agent execution")
            item.validate()
            if self.plan is not None and item.step_id not in {step.step_id for step in self.plan.steps}:
                raise ValueError("Agent execution references an unknown step")
        for item in self.verification_results:
            if not isinstance(item, VerificationResult):
                raise ValueError("Invalid agent verification result")
            item.validate()
        if self.verification_plan is not None and any(
            result.run_id == self.verification_plan.run_id
            and result.check_id is not None
            and result.check_id not in {
                check.check_id for check in self.verification_plan.checks
            }
            for result in self.verification_results
        ):
            raise ValueError("Verification result references an unknown check")
        for item in self.repair_attempts:
            if not isinstance(item, RepairAttempt):
                raise ValueError("Invalid agent repair attempt")
            item.validate()
            if item.verification_index is not None and item.verification_index >= len(self.verification_results):
                raise ValueError("Repair attempt references an unknown verification result")
            if any(
                execution_id not in {record.execution_id for record in self.executions}
                for execution_id in item.execution_ids
            ):
                raise ValueError("Repair attempt references an unknown execution")
        if any(execution.status == ExecutionStatus.PENDING for execution in self.executions):
            if self.status in TERMINAL_STATUSES or self.status not in _ACTIVE_STATUSES:
                raise ValueError("Uncertain execution is attached to an inactive agent task")

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return {
            "task_id": self.task_id, "goal": self.goal, "status": self.status.value,
            "created_at": self.created_at, "updated_at": self.updated_at,
            "selected_model": self.selected_model,
            "plan": self.plan.to_dict() if self.plan is not None else None,
            "current_step_id": self.current_step_id,
            "executions": [item.to_dict() for item in self.executions],
            "verification_results": [item.to_dict() for item in self.verification_results],
            "verification_plan": (
                self.verification_plan.to_dict() if self.verification_plan is not None else None
            ),
            "verification_outcome": (
                self.verification_outcome.value if self.verification_outcome is not None else None
            ),
            "current_verification_check_id": self.current_verification_check_id,
            "repair_attempts": [item.to_dict() for item in self.repair_attempts],
            "repair_outcome": self.repair_outcome.value if self.repair_outcome else None,
            "review_record": self.review_record.to_dict() if self.review_record else None,
            "terminal_summary": self.terminal_summary,
            "approval_resume_state": (
                self.approval_resume_state.value if self.approval_resume_state is not None else None
            ),
        }

    @classmethod
    def from_dict(cls, value: object) -> AgentTask:
        legacy_keys = {
            "task_id", "goal", "status", "created_at", "updated_at", "selected_model",
            "plan", "current_step_id", "executions", "verification_results",
            "repair_attempts", "terminal_summary", "approval_resume_state",
        }
        extended_keys = legacy_keys | {
            "verification_plan", "verification_outcome", "current_verification_check_id",
        }
        repair_keys = extended_keys | {"repair_outcome"}
        review_keys = repair_keys | {"review_record"}
        if not isinstance(value, dict) or set(value) not in {
            frozenset(legacy_keys), frozenset(extended_keys), frozenset(repair_keys),
            frozenset(review_keys),
        }:
            raise ValueError("Invalid agent task fields")
        data = value
        for key in ("executions", "verification_results", "repair_attempts"):
            if not isinstance(data[key], list):
                raise ValueError(f"Invalid agent task {key}")
        try:
            result = cls(
                goal=data["goal"], task_id=data["task_id"], status=AgentStatus(data["status"]),
                created_at=data["created_at"], updated_at=data["updated_at"],
                selected_model=data["selected_model"],
                plan=AgentPlan.from_dict(data["plan"]) if data["plan"] is not None else None,
                current_step_id=data["current_step_id"],
                executions=[AgentExecution.from_dict(item) for item in data["executions"]],
                verification_results=[
                    VerificationResult.from_dict(item) for item in data["verification_results"]
                ],
                verification_plan=(
                    VerificationPlan.from_dict(data["verification_plan"])
                    if data.get("verification_plan") is not None else None
                ),
                verification_outcome=(
                    VerificationOutcome(data["verification_outcome"])
                    if data.get("verification_outcome") is not None else None
                ),
                current_verification_check_id=data.get("current_verification_check_id"),
                repair_attempts=[RepairAttempt.from_dict(item) for item in data["repair_attempts"]],
                repair_outcome=(
                    RepairOutcome(data["repair_outcome"])
                    if data.get("repair_outcome") is not None else None
                ),
                review_record=(
                    ReviewRecord.from_dict(data["review_record"])
                    if data.get("review_record") is not None else None
                ),
                terminal_summary=data["terminal_summary"],
                approval_resume_state=(
                    AgentStatus(data["approval_resume_state"])
                    if data["approval_resume_state"] is not None else None
                ),
            )
            result.validate()
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Invalid agent task: {exc}") from exc
        return result


@dataclass
class AgentCheckpoint:
    task: AgentTask
    checkpointed_at: str = field(default_factory=_now)
    version: int = 1

    def validate(self) -> None:
        if type(self.version) is not int or self.version != 1:
            raise ValueError("Unsupported agent checkpoint version")
        _timestamp(self.checkpointed_at, "checkpoint timestamp")
        if not isinstance(self.task, AgentTask):
            raise ValueError("Invalid agent checkpoint task")
        self.task.validate()
        try:
            payload_size = len(json.dumps(self.task.to_dict(), ensure_ascii=True).encode("utf-8"))
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError(f"Invalid agent checkpoint data: {exc}") from exc
        if payload_size > 4 * 1024 * 1024:
            raise ValueError("Agent checkpoint exceeds 4 MiB")

    def recover_interrupted(self) -> bool:
        changed = self.task.recover_interrupted()
        if changed:
            self.checkpointed_at = _now()
        return changed

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return {"version": self.version, "checkpointed_at": self.checkpointed_at, "task": self.task.to_dict()}

    @classmethod
    def from_dict(cls, value: object) -> AgentCheckpoint:
        data = _object(value, "checkpoint", {"version", "checkpointed_at", "task"})
        try:
            result = cls(
                task=AgentTask.from_dict(data["task"]),
                checkpointed_at=data["checkpointed_at"], version=data["version"],
            )
            result.validate()
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Invalid agent checkpoint: {exc}") from exc
        return result
