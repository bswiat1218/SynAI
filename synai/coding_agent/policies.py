from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any


POLICY_VERSION = 1
_FINGERPRINT = re.compile(r"^[a-f0-9]{64}$")


class AutonomyMode(StrEnum):
    SUPERVISED = "supervised"
    AGENT = "agent"
    AUTONOMOUS = "autonomous"


class OperationCategory(StrEnum):
    REPOSITORY_READ = "repository_read"
    REPOSITORY_INTELLIGENCE = "repository_intelligence"
    FILE_READ = "file_read"
    FILE_CREATION = "file_creation"
    FILE_MODIFICATION = "file_modification"
    FILE_DELETION = "file_deletion"
    TERMINAL_EXECUTION = "terminal_execution"
    VERIFICATION_EXECUTION = "verification_execution"
    NETWORK_ACCESS = "network_access"
    GIT_INSPECTION = "git_inspection"
    GIT_CHECKPOINT_CREATION = "git_checkpoint_creation"
    CHECKPOINT_RESTORATION = "checkpoint_restoration"
    UNKNOWN = "unknown"


class PolicyDecisionType(StrEnum):
    ALLOW = "allow"
    REQUIRE_APPROVAL = "require_approval"
    DENY = "deny"


class PolicyReason(StrEnum):
    SAFE_READ_ALLOWED = "safe_read_allowed"
    EXPLICIT_ELIGIBLE_EXEMPTION = "explicit_eligible_exemption"
    EXPLICIT_TOOL_DENY = "explicit_tool_deny"
    EXPLICIT_CATEGORY_DENY = "explicit_category_deny"
    TOOL_APPROVAL_REQUIRED = "tool_approval_required"
    FILE_MUTATION_APPROVAL_REQUIRED = "file_mutation_approval_required"
    DELETION_APPROVAL_REQUIRED = "deletion_approval_required"
    TERMINAL_APPROVAL_REQUIRED = "terminal_approval_required"
    NETWORK_APPROVAL_REQUIRED = "network_approval_required"
    GIT_APPROVAL_REQUIRED = "git_approval_required"
    CHECKPOINT_APPROVAL_REQUIRED = "checkpoint_approval_required"
    UNKNOWN_OPERATION = "unknown_operation"
    INVALID_POLICY_CONTEXT = "invalid_policy_context"
    INVALID_ARGUMENTS = "invalid_arguments"
    HARD_SECURITY_RESTRICTION = "hard_security_restriction"
    WORKSPACE_MISMATCH = "workspace_mismatch"
    BACKEND_MISMATCH = "backend_mismatch"
    PLAN_SCOPE_VIOLATION = "plan_scope_violation"
    TASK_INACTIVE = "task_inactive"
    CANCELLED = "cancelled"
    RESOURCE_LIMIT = "resource_limit"
    MODE_NOT_PERMITTED = "mode_not_permitted"
    POLICY_CHANGED = "policy_changed"
    POLICY_EVALUATION_ERROR = "policy_evaluation_error"


class PolicySource(StrEnum):
    BUILT_IN_DEFAULT = "built_in_default"
    TRUSTED_USER_CONFIGURATION = "trusted_user_configuration"
    HARD_SECURITY_POLICY = "hard_security_policy"


_ELIGIBLE_EXEMPTION_TOOLS = frozenset({
    "get_project_structure",
    "find_symbol",
    "find_definition",
    "find_references",
    "find_callers",
    "find_implementations",
    "find_imports",
    "find_tests",
    "search_code",
    "get_diagnostics",
})
_REGISTERED_TOOL_NAMES = frozenset({
    "read_file", "list_files", "write_file", "patch_file", "delete_file",
    "terminal", "fetch_url", *_ELIGIBLE_EXEMPTION_TOOLS,
    "git_status", "git_diff", "git_log", "git_show", "git_checkpoint",
    "restore_checkpoint",
})
_CATEGORY_DENYABLE = frozenset(OperationCategory) - {OperationCategory.UNKNOWN}


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def argument_fingerprint(arguments: dict[str, Any]) -> str:
    encoded = json.dumps(
        arguments, ensure_ascii=True, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def workspace_fingerprint(workspace: str) -> str:
    return hashlib.sha256(workspace.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class AutonomyPolicyConfig:
    schema_version: int = POLICY_VERSION
    default_mode: AutonomyMode = AutonomyMode.AGENT
    permitted_modes: tuple[AutonomyMode, ...] = tuple(AutonomyMode)
    denied_tools: tuple[str, ...] = ()
    denied_categories: tuple[OperationCategory, ...] = ()
    automatic_approval_exemptions: tuple[str, ...] = ()

    def validate(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != POLICY_VERSION:
            raise ValueError("Unsupported autonomy policy configuration version")
        if not isinstance(self.default_mode, AutonomyMode):
            raise ValueError("Autonomy policy default mode is invalid")
        if (
            not isinstance(self.permitted_modes, tuple)
            or not self.permitted_modes
            or any(not isinstance(item, AutonomyMode) for item in self.permitted_modes)
            or len(set(self.permitted_modes)) != len(self.permitted_modes)
            or self.default_mode not in self.permitted_modes
        ):
            raise ValueError("Permitted autonomy modes are invalid")
        if (
            not isinstance(self.denied_tools, tuple)
            or any(
                not isinstance(item, str) or item not in _REGISTERED_TOOL_NAMES
                for item in self.denied_tools
            )
            or len(set(self.denied_tools)) != len(self.denied_tools)
        ):
            raise ValueError("Policy tool denies must name unique registered tools")
        if (
            not isinstance(self.denied_categories, tuple)
            or any(
                not isinstance(item, OperationCategory) or item not in _CATEGORY_DENYABLE
                for item in self.denied_categories
            )
            or len(set(self.denied_categories)) != len(self.denied_categories)
        ):
            raise ValueError("Policy category denies must name unique known categories")
        if (
            not isinstance(self.automatic_approval_exemptions, tuple)
            or any(
                not isinstance(item, str) or item not in _ELIGIBLE_EXEMPTION_TOOLS
                for item in self.automatic_approval_exemptions
            )
            or len(set(self.automatic_approval_exemptions))
            != len(self.automatic_approval_exemptions)
        ):
            raise ValueError("Automatic exemptions must name unique eligible intelligence tools")
        if set(self.denied_tools) & set(self.automatic_approval_exemptions):
            raise ValueError("A denied tool cannot also have an automatic exemption")
        denied_categories = set(self.denied_categories)
        if any(
            classify_operation(name, {}) in denied_categories
            for name in self.automatic_approval_exemptions
        ):
            raise ValueError("An exempted tool cannot belong to a denied category")

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return {
            "schema_version": self.schema_version,
            "default_mode": self.default_mode.value,
            "permitted_modes": [item.value for item in self.permitted_modes],
            "denied_tools": list(self.denied_tools),
            "denied_categories": [item.value for item in self.denied_categories],
            "automatic_approval_exemptions": list(self.automatic_approval_exemptions),
        }

    @classmethod
    def from_dict(cls, value: object) -> AutonomyPolicyConfig:
        keys = {
            "schema_version", "default_mode", "permitted_modes", "denied_tools",
            "denied_categories", "automatic_approval_exemptions",
        }
        if not isinstance(value, dict) or set(value) != keys:
            raise ValueError("Invalid autonomy policy configuration fields")
        if any(not isinstance(value[key], list) for key in (
            "permitted_modes", "denied_tools", "denied_categories",
            "automatic_approval_exemptions",
        )):
            raise ValueError("Autonomy policy rule collections must be lists")
        try:
            result = cls(
                schema_version=value["schema_version"],
                default_mode=AutonomyMode(value["default_mode"]),
                permitted_modes=tuple(AutonomyMode(item) for item in value["permitted_modes"]),
                denied_tools=tuple(value["denied_tools"]),
                denied_categories=tuple(
                    OperationCategory(item) for item in value["denied_categories"]
                ),
                automatic_approval_exemptions=tuple(value["automatic_approval_exemptions"]),
            )
            result.validate()
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Invalid autonomy policy configuration: {exc}") from exc
        return result


@dataclass(frozen=True)
class PolicyTaskContext:
    mode: AutonomyMode
    policy_version: int
    policy_fingerprint: str
    workspace_identity: str
    backend_identity: str
    created_at: str = ""

    def validate(self) -> None:
        if not isinstance(self.mode, AutonomyMode):
            raise ValueError("Invalid task autonomy mode")
        if type(self.policy_version) is not int or self.policy_version != POLICY_VERSION:
            raise ValueError("Unsupported task policy version")
        if not isinstance(self.policy_fingerprint, str) or not _FINGERPRINT.fullmatch(
            self.policy_fingerprint,
        ):
            raise ValueError("Invalid task policy fingerprint")
        if not isinstance(self.workspace_identity, str) or not _FINGERPRINT.fullmatch(
            self.workspace_identity,
        ):
            raise ValueError("Invalid task workspace identity")
        if self.backend_identity not in {"sandbox", "host"}:
            raise ValueError("Invalid task execution backend identity")
        if self.created_at and (
            not isinstance(self.created_at, str) or len(self.created_at) > 64
        ):
            raise ValueError("Invalid task policy timestamp")

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return {
            "mode": self.mode.value,
            "policy_version": self.policy_version,
            "policy_fingerprint": self.policy_fingerprint,
            "workspace_identity": self.workspace_identity,
            "backend_identity": self.backend_identity,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, value: object) -> PolicyTaskContext:
        keys = {
            "mode", "policy_version", "policy_fingerprint", "workspace_identity",
            "backend_identity", "created_at",
        }
        if not isinstance(value, dict) or set(value) != keys:
            raise ValueError("Invalid task policy context fields")
        try:
            result = cls(
                mode=AutonomyMode(value["mode"]),
                policy_version=value["policy_version"],
                policy_fingerprint=value["policy_fingerprint"],
                workspace_identity=value["workspace_identity"],
                backend_identity=value["backend_identity"],
                created_at=value["created_at"],
            )
            result.validate()
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Invalid task policy context: {exc}") from exc
        return result


@dataclass(frozen=True)
class PolicyAuditRecord:
    task_id: str
    step_id: str | None
    execution_id: str | None
    tool_name: str
    category: OperationCategory
    mode: AutonomyMode
    decision: PolicyDecisionType
    reason: PolicyReason
    policy_fingerprint: str
    approval_required: bool
    approval_outcome: str
    backend_identity: str
    workspace_identity: str
    timestamp: str

    def validate(self) -> None:
        if not isinstance(self.task_id, str) or not 1 <= len(self.task_id) <= 128:
            raise ValueError("Invalid policy audit task ID")
        for value, label, limit in (
            (self.step_id, "step ID", 128),
            (self.execution_id, "execution ID", 128),
        ):
            if value is not None and (
                not isinstance(value, str) or not 1 <= len(value) <= limit
            ):
                raise ValueError(f"Invalid policy audit {label}")
        if (
            not isinstance(self.tool_name, str)
            or self.tool_name not in _REGISTERED_TOOL_NAMES
            or not isinstance(self.category, OperationCategory)
            or not isinstance(self.mode, AutonomyMode)
            or not isinstance(self.decision, PolicyDecisionType)
            or not isinstance(self.reason, PolicyReason)
        ):
            raise ValueError("Invalid policy audit classification")
        if not isinstance(self.policy_fingerprint, str) or not _FINGERPRINT.fullmatch(
            self.policy_fingerprint,
        ):
            raise ValueError("Invalid policy audit fingerprint")
        if type(self.approval_required) is not bool:
            raise ValueError("Invalid policy audit approval state")
        if self.approval_outcome not in {
            "not_required", "pending", "approved", "denied",
            "cancelled", "policy_denied", "never_dispatched",
        }:
            raise ValueError("Invalid policy audit approval outcome")
        if self.backend_identity not in {"sandbox", "host"}:
            raise ValueError("Invalid policy audit backend")
        if not isinstance(self.workspace_identity, str) or not _FINGERPRINT.fullmatch(
            self.workspace_identity,
        ):
            raise ValueError("Invalid policy audit workspace reference")
        if not isinstance(self.timestamp, str) or not 1 <= len(self.timestamp) <= 64:
            raise ValueError("Invalid policy audit timestamp")

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return {
            "task_id": self.task_id,
            "step_id": self.step_id,
            "execution_id": self.execution_id,
            "tool_name": self.tool_name,
            "category": self.category.value,
            "mode": self.mode.value,
            "decision": self.decision.value,
            "reason": self.reason.value,
            "policy_fingerprint": self.policy_fingerprint,
            "approval_required": self.approval_required,
            "approval_outcome": self.approval_outcome,
            "backend_identity": self.backend_identity,
            "workspace_identity": self.workspace_identity,
            "timestamp": self.timestamp,
        }

    @classmethod
    def from_dict(cls, value: object) -> PolicyAuditRecord:
        keys = {
            "task_id", "step_id", "execution_id", "tool_name", "category",
            "mode", "decision", "reason", "policy_fingerprint",
            "approval_required", "approval_outcome", "backend_identity",
            "workspace_identity", "timestamp",
        }
        if not isinstance(value, dict) or set(value) != keys:
            raise ValueError("Invalid policy audit record fields")
        try:
            result = cls(
                task_id=value["task_id"],
                step_id=value["step_id"],
                execution_id=value["execution_id"],
                tool_name=value["tool_name"],
                category=OperationCategory(value["category"]),
                mode=AutonomyMode(value["mode"]),
                decision=PolicyDecisionType(value["decision"]),
                reason=PolicyReason(value["reason"]),
                policy_fingerprint=value["policy_fingerprint"],
                approval_required=value["approval_required"],
                approval_outcome=value["approval_outcome"],
                backend_identity=value["backend_identity"],
                workspace_identity=value["workspace_identity"],
                timestamp=value["timestamp"],
            )
            result.validate()
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Invalid policy audit record: {exc}") from exc
        return result


@dataclass(frozen=True)
class PolicyRequest:
    tool_name: str
    arguments_fingerprint: str
    category: OperationCategory
    mode: AutonomyMode
    task_id: str
    step_id: str | None
    backend_identity: str
    workspace: str
    policy_fingerprint: str
    backend_valid: bool = True
    workspace_valid: bool = True
    arguments_valid: bool = True
    hard_security_allowed: bool = True
    plan_scope_valid: bool = True
    task_active: bool = True
    resource_available: bool = True

    def validate(self) -> None:
        if (
            not isinstance(self.tool_name, str)
            or not 1 <= len(self.tool_name) <= 128
        ):
            raise ValueError("Policy request tool name is invalid")
        if not isinstance(self.arguments_fingerprint, str) or not _FINGERPRINT.fullmatch(
            self.arguments_fingerprint,
        ):
            raise ValueError("Policy request arguments are not fingerprinted")
        if not isinstance(self.category, OperationCategory):
            raise ValueError("Policy request category is invalid")
        if not isinstance(self.mode, AutonomyMode):
            raise ValueError("Policy request mode is invalid")
        if not isinstance(self.task_id, str) or not 1 <= len(self.task_id) <= 128:
            raise ValueError("Policy request task ID is invalid")
        if self.step_id is not None and (
            not isinstance(self.step_id, str) or not 1 <= len(self.step_id) <= 128
        ):
            raise ValueError("Policy request step ID is invalid")
        if self.backend_identity not in {"sandbox", "host"}:
            raise ValueError("Policy request backend identity is invalid")
        if not isinstance(self.workspace, str) or len(self.workspace) > 4096:
            raise ValueError("Policy request workspace is invalid")
        if not isinstance(self.policy_fingerprint, str) or not _FINGERPRINT.fullmatch(
            self.policy_fingerprint,
        ):
            raise ValueError("Policy request fingerprint is invalid")
        if any(type(value) is not bool for value in (
            self.backend_valid, self.workspace_valid, self.arguments_valid,
            self.hard_security_allowed, self.plan_scope_valid, self.task_active,
            self.resource_available,
        )):
            raise ValueError("Policy request constraints must be booleans")


@dataclass(frozen=True)
class PolicyDecision:
    decision: PolicyDecisionType
    reason: PolicyReason
    explanation: str
    mode: AutonomyMode
    category: OperationCategory
    tool_name: str
    source: PolicySource
    constraints: tuple[str, ...]
    explicit_configuration: bool
    policy_fingerprint: str

    def validate(self) -> None:
        if not isinstance(self.decision, PolicyDecisionType):
            raise ValueError("Invalid policy decision")
        if not isinstance(self.reason, PolicyReason):
            raise ValueError("Invalid policy decision reason")
        if not isinstance(self.explanation, str) or not 1 <= len(self.explanation) <= 512:
            raise ValueError("Invalid policy decision explanation")
        if not isinstance(self.mode, AutonomyMode):
            raise ValueError("Invalid policy decision mode")
        if not isinstance(self.category, OperationCategory):
            raise ValueError("Invalid policy decision category")
        if not isinstance(self.tool_name, str) or len(self.tool_name) > 128:
            raise ValueError("Invalid policy decision tool name")
        if not isinstance(self.source, PolicySource):
            raise ValueError("Invalid policy decision source")
        if (
            not isinstance(self.constraints, tuple)
            or len(self.constraints) > 16
            or any(not isinstance(item, str) or len(item) > 128 for item in self.constraints)
        ):
            raise ValueError("Invalid policy decision constraints")
        if type(self.explicit_configuration) is not bool:
            raise ValueError("Invalid explicit policy configuration flag")
        if not isinstance(self.policy_fingerprint, str) or not _FINGERPRINT.fullmatch(
            self.policy_fingerprint,
        ):
            raise ValueError("Invalid policy decision fingerprint")


def classify_operation(
    tool_name: str,
    arguments: dict[str, Any],
    *,
    target_exists: bool | None = None,
    category_override: OperationCategory | None = None,
) -> OperationCategory:
    """Classify only exact registered names; model-provided labels are ignored."""
    if category_override is not None:
        valid_override = {
            "terminal": {
                OperationCategory.TERMINAL_EXECUTION,
                OperationCategory.VERIFICATION_EXECUTION,
            },
            "write_file": {OperationCategory.FILE_CREATION, OperationCategory.FILE_MODIFICATION},
            "patch_file": {OperationCategory.FILE_MODIFICATION},
            "delete_file": {OperationCategory.FILE_DELETION},
        }
        if tool_name in valid_override:
            return (
                category_override
                if category_override in valid_override[tool_name]
                else OperationCategory.UNKNOWN
            )
        classified = classify_operation(
            tool_name, arguments, target_exists=target_exists,
        )
        return category_override if classified == category_override else OperationCategory.UNKNOWN
    if tool_name == "read_file":
        return OperationCategory.FILE_READ
    if tool_name == "list_files":
        return OperationCategory.REPOSITORY_READ
    if tool_name in _ELIGIBLE_EXEMPTION_TOOLS:
        return OperationCategory.REPOSITORY_INTELLIGENCE
    if tool_name == "write_file":
        return (
            OperationCategory.FILE_MODIFICATION
            if target_exists is not False else OperationCategory.FILE_CREATION
        )
    if tool_name == "patch_file":
        return OperationCategory.FILE_MODIFICATION
    if tool_name == "delete_file":
        return OperationCategory.FILE_DELETION
    if tool_name == "terminal":
        return OperationCategory.TERMINAL_EXECUTION
    if tool_name == "fetch_url":
        return OperationCategory.NETWORK_ACCESS
    if tool_name in {"git_status", "git_diff", "git_log", "git_show"}:
        return OperationCategory.GIT_INSPECTION
    if tool_name == "git_checkpoint":
        return OperationCategory.GIT_CHECKPOINT_CREATION
    if tool_name == "restore_checkpoint":
        return OperationCategory.CHECKPOINT_RESTORATION
    return OperationCategory.UNKNOWN


class AutonomyPolicy:
    """Deterministic, fail-closed policy evaluator; it never dispatches tools."""

    def __init__(self, configuration: AutonomyPolicyConfig | None = None) -> None:
        self._configuration = configuration or AutonomyPolicyConfig()
        self._configuration.validate()

    @property
    def configuration(self) -> AutonomyPolicyConfig:
        return self._configuration

    @property
    def fingerprint(self) -> str:
        encoded = json.dumps(
            self._configuration.to_dict(),
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def replace_configuration(self, configuration: AutonomyPolicyConfig) -> None:
        configuration.validate()
        self._configuration = configuration

    def create_task_context(
        self,
        mode: AutonomyMode,
        workspace: str,
        backend_identity: str,
    ) -> PolicyTaskContext:
        if mode not in self._configuration.permitted_modes:
            raise ValueError("Selected autonomy mode is not permitted by trusted policy")
        context = PolicyTaskContext(
            mode=mode,
            policy_version=POLICY_VERSION,
            policy_fingerprint=self.fingerprint,
            workspace_identity=workspace_fingerprint(workspace),
            backend_identity=backend_identity,
            created_at=_timestamp(),
        )
        context.validate()
        return context

    def create_request(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        mode: AutonomyMode,
        task_id: str,
        step_id: str | None,
        backend_identity: str,
        workspace: str,
        policy_fingerprint: str | None = None,
        category: OperationCategory | None = None,
        target_exists: bool | None = None,
        workspace_valid: bool = True,
        hard_security_allowed: bool = True,
        plan_scope_valid: bool = True,
        task_active: bool = True,
        resource_available: bool = True,
    ) -> PolicyRequest:
        request = PolicyRequest(
            tool_name=tool_name,
            arguments_fingerprint=argument_fingerprint(arguments),
            category=classify_operation(
                tool_name, arguments, target_exists=target_exists,
                category_override=category,
            ),
            mode=mode,
            task_id=task_id,
            step_id=step_id,
            backend_identity=backend_identity,
            workspace=workspace,
            policy_fingerprint=policy_fingerprint or self.fingerprint,
            workspace_valid=workspace_valid,
            hard_security_allowed=hard_security_allowed,
            plan_scope_valid=plan_scope_valid,
            task_active=task_active,
            resource_available=resource_available,
        )
        request.validate()
        return request

    def evaluate(self, request: PolicyRequest, *, cancelled: bool = False) -> PolicyDecision:
        try:
            request.validate()
            return self._evaluate(request, cancelled=cancelled)
        except Exception:
            return self._decision(
                PolicyDecisionType.DENY,
                PolicyReason.POLICY_EVALUATION_ERROR,
                "Policy evaluation failed; the operation is denied.",
                mode=request.mode if isinstance(request, PolicyRequest) and isinstance(
                    request.mode, AutonomyMode,
                ) else AutonomyMode.SUPERVISED,
                category=request.category if isinstance(request, PolicyRequest) and isinstance(
                    request.category, OperationCategory,
                ) else OperationCategory.UNKNOWN,
                tool_name=request.tool_name[:128] if isinstance(
                    request, PolicyRequest,
                ) and isinstance(request.tool_name, str) else "unknown",
                source=PolicySource.HARD_SECURITY_POLICY,
                constraints=("policy_evaluation",),
                explicit_configuration=False,
            )

    def _evaluate(self, request: PolicyRequest, *, cancelled: bool) -> PolicyDecision:
        if not request.hard_security_allowed:
            return self._decision(
                PolicyDecisionType.DENY, PolicyReason.HARD_SECURITY_RESTRICTION,
                "A hard security restriction blocks this operation.",
                request, PolicySource.HARD_SECURITY_POLICY, ("hard_security",),
            )
        if request.backend_identity not in {"sandbox", "host"}:
            return self._decision(
                PolicyDecisionType.DENY, PolicyReason.BACKEND_MISMATCH,
                "Execution backend identity is unavailable.",
                request, PolicySource.HARD_SECURITY_POLICY, ("backend_identity",),
            )
        if not request.backend_valid:
            return self._decision(
                PolicyDecisionType.DENY, PolicyReason.BACKEND_MISMATCH,
                "The active execution backend changed during this task.",
                request, PolicySource.HARD_SECURITY_POLICY, ("backend_identity",),
            )
        if not request.workspace_valid:
            return self._decision(
                PolicyDecisionType.DENY, PolicyReason.WORKSPACE_MISMATCH,
                "The active workspace identity is invalid or changed; a matching active conversation workspace is required.",
                request, PolicySource.HARD_SECURITY_POLICY, ("workspace_identity",),
            )
        if not request.workspace.strip():
            return self._decision(
                PolicyDecisionType.DENY, PolicyReason.WORKSPACE_MISMATCH,
                "The active workspace identity is unavailable.",
                request, PolicySource.HARD_SECURITY_POLICY, ("workspace_identity",),
            )
        if not request.arguments_valid:
            return self._decision(
                PolicyDecisionType.DENY, PolicyReason.INVALID_ARGUMENTS,
                "Tool arguments did not pass strict validation.",
                request, PolicySource.HARD_SECURITY_POLICY, ("validated_arguments",),
            )
        if request.category == OperationCategory.UNKNOWN:
            return self._decision(
                PolicyDecisionType.DENY, PolicyReason.UNKNOWN_OPERATION,
                "The operation is unknown and is denied by default.",
                request, PolicySource.HARD_SECURITY_POLICY, ("known_operation",),
            )
        if not request.plan_scope_valid:
            return self._decision(
                PolicyDecisionType.DENY, PolicyReason.PLAN_SCOPE_VIOLATION,
                "The operation is outside the validated plan scope.",
                request, PolicySource.HARD_SECURITY_POLICY, ("plan_scope",),
            )
        if not request.task_active:
            return self._decision(
                PolicyDecisionType.DENY, PolicyReason.TASK_INACTIVE,
                "The Agent Task or active step is no longer eligible to execute.",
                request, PolicySource.HARD_SECURITY_POLICY, ("task_state",),
            )
        if cancelled:
            return self._decision(
                PolicyDecisionType.DENY, PolicyReason.CANCELLED,
                "The task was cancelled before dispatch.",
                request, PolicySource.HARD_SECURITY_POLICY, ("cancellation",),
            )
        if not request.resource_available:
            return self._decision(
                PolicyDecisionType.DENY, PolicyReason.RESOURCE_LIMIT,
                "An execution resource limit has been reached.",
                request, PolicySource.HARD_SECURITY_POLICY, ("resource_limit",),
            )
        if request.policy_fingerprint != self.fingerprint:
            return self._decision(
                PolicyDecisionType.DENY, PolicyReason.POLICY_CHANGED,
                "The effective policy changed during this task; revalidation is required.",
                request, PolicySource.HARD_SECURITY_POLICY, ("policy_fingerprint",),
            )
        if (
            request.task_id != "dispatcher"
            and request.mode not in self._configuration.permitted_modes
        ):
            return self._decision(
                PolicyDecisionType.DENY, PolicyReason.MODE_NOT_PERMITTED,
                "The selected autonomy mode is not permitted by trusted configuration.",
                request, PolicySource.TRUSTED_USER_CONFIGURATION, ("permitted_modes",),
                explicit=True,
            )
        if request.tool_name in self._configuration.denied_tools:
            return self._decision(
                PolicyDecisionType.DENY, PolicyReason.EXPLICIT_TOOL_DENY,
                "Trusted configuration explicitly denies this tool.",
                request, PolicySource.TRUSTED_USER_CONFIGURATION, ("denied_tools",),
                explicit=True,
            )
        if request.category in self._configuration.denied_categories:
            return self._decision(
                PolicyDecisionType.DENY, PolicyReason.EXPLICIT_CATEGORY_DENY,
                "Trusted configuration explicitly denies this operation category.",
                request, PolicySource.TRUSTED_USER_CONFIGURATION, ("denied_categories",),
                explicit=True,
            )
        if request.category in {
            OperationCategory.FILE_CREATION, OperationCategory.FILE_MODIFICATION,
        }:
            return self._approval(
                PolicyReason.FILE_MUTATION_APPROVAL_REQUIRED,
                "File creation and modification require the existing per-operation approval.",
                request, ("tool_approval", "workspace_mutation"),
            )
        if request.category == OperationCategory.FILE_DELETION:
            return self._approval(
                PolicyReason.DELETION_APPROVAL_REQUIRED,
                "File deletion requires the existing explicit approval.",
                request, ("tool_approval", "destructive_operation"),
            )
        if request.category in {
            OperationCategory.TERMINAL_EXECUTION,
            OperationCategory.VERIFICATION_EXECUTION,
        }:
            reason = (
                PolicyReason.TERMINAL_APPROVAL_REQUIRED
                if request.category == OperationCategory.TERMINAL_EXECUTION
                else PolicyReason.TOOL_APPROVAL_REQUIRED
            )
            return self._approval(
                reason,
                "Terminal execution retains the existing approval and backend restrictions.",
                request, ("tool_approval", "backend_command_restrictions"),
            )
        if request.category == OperationCategory.NETWORK_ACCESS:
            return self._approval(
                PolicyReason.NETWORK_APPROVAL_REQUIRED,
                "Network access requires the existing explicit approval.",
                request, ("tool_approval", "network_access"),
            )
        if request.category == OperationCategory.GIT_INSPECTION:
            return self._approval(
                PolicyReason.GIT_APPROVAL_REQUIRED,
                "Git inspection retains its existing terminal approval.",
                request, ("tool_approval", "workspace_scoped_git"),
            )
        if request.category in {
            OperationCategory.GIT_CHECKPOINT_CREATION,
            OperationCategory.CHECKPOINT_RESTORATION,
        }:
            return self._approval(
                PolicyReason.CHECKPOINT_APPROVAL_REQUIRED,
                "Checkpoint creation and restoration retain explicit approval and integrity checks.",
                request, ("tool_approval", "checkpoint_integrity"),
            )
        if request.category in {
            OperationCategory.FILE_READ,
            OperationCategory.REPOSITORY_READ,
        }:
            return self._decision(
                PolicyDecisionType.ALLOW, PolicyReason.SAFE_READ_ALLOWED,
                "This bounded workspace read follows the existing read policy.",
                request, PolicySource.BUILT_IN_DEFAULT, ("workspace_boundary",),
            )
        if request.category == OperationCategory.REPOSITORY_INTELLIGENCE:
            explicit = (
                request.task_id != "dispatcher"
                and request.mode == AutonomyMode.AUTONOMOUS
                and request.backend_identity == "sandbox"
                and request.tool_name in self._configuration.automatic_approval_exemptions
            )
            return self._decision(
                PolicyDecisionType.ALLOW,
                PolicyReason.EXPLICIT_ELIGIBLE_EXEMPTION if explicit
                else PolicyReason.SAFE_READ_ALLOWED,
                "This bounded repository-intelligence operation is permitted; existing workspace and budget checks still apply.",
                request,
                PolicySource.TRUSTED_USER_CONFIGURATION if explicit
                else PolicySource.BUILT_IN_DEFAULT,
                ("workspace_boundary", "tool_budget"),
                explicit=explicit,
            )
        return self._decision(
            PolicyDecisionType.DENY, PolicyReason.UNKNOWN_OPERATION,
            "No safe policy rule applies to this operation.",
            request, PolicySource.HARD_SECURITY_POLICY, ("known_operation",),
        )

    def _approval(
        self,
        reason: PolicyReason,
        explanation: str,
        request: PolicyRequest,
        constraints: tuple[str, ...],
    ) -> PolicyDecision:
        return self._decision(
            PolicyDecisionType.REQUIRE_APPROVAL, reason, explanation, request,
            PolicySource.BUILT_IN_DEFAULT, constraints,
        )

    def _decision(
        self,
        decision: PolicyDecisionType,
        reason: PolicyReason,
        explanation: str,
        request: PolicyRequest | None = None,
        source: PolicySource = PolicySource.BUILT_IN_DEFAULT,
        constraints: tuple[str, ...] = (),
        *,
        mode: AutonomyMode = AutonomyMode.AGENT,
        category: OperationCategory = OperationCategory.UNKNOWN,
        tool_name: str = "unknown",
        explicit: bool = False,
    ) -> PolicyDecision:
        if request is not None:
            mode, category, tool_name = request.mode, request.category, request.tool_name
        result = PolicyDecision(
            decision=decision,
            reason=reason,
            explanation=explanation[:512],
            mode=mode,
            category=category,
            tool_name=tool_name[:128],
            source=source,
            constraints=constraints,
            explicit_configuration=explicit,
            policy_fingerprint=self.fingerprint,
        )
        result.validate()
        return result


def inspect_policy(decision: PolicyDecision) -> str:
    decision.validate()
    return "\n".join((
        f"Decision: {decision.decision.value.upper()}",
        f"Mode: {decision.mode.value.upper()}",
        f"Operation: {decision.tool_name} ({decision.category.value})",
        f"Reason: {decision.reason.value}",
        f"Policy source: {decision.source.value.replace('_', ' ').title()}",
        f"Explanation: {decision.explanation}",
    ))
