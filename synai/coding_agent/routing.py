from __future__ import annotations

import asyncio
import hashlib
import json
import re
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Awaitable
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

if TYPE_CHECKING:
    from synai.models import ModelInfo
    from synai.providers.base import ModelProvider

from synai.providers.errors import ModelCapabilityMetadataError, ModelUnavailableError


class RoutingMode(StrEnum):
    SINGLE_MODEL = "single_model"
    ROUTED = "routed"


class ModelRole(StrEnum):
    PLANNING = "planning"
    IMPLEMENTATION = "implementation"
    REPAIR = "repair"
    REVIEW = "review"


class RoutingStrategy(StrEnum):
    PINNED = "pinned"
    BALANCED = "balanced"
    CAPABILITY_FIRST = "capability_first"


class PreferenceTier(StrEnum):
    SMALL = "small"
    MEDIUM = "medium"
    LARGE = "large"
    UNKNOWN = "unknown"


class ComplexityTier(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    UNKNOWN = "unknown"


class RoutingErrorCode(StrEnum):
    MODEL_ROUTING_DISABLED = "MODEL_ROUTING_DISABLED"
    INVALID_ROUTING_CONFIGURATION = "INVALID_ROUTING_CONFIGURATION"
    NO_ELIGIBLE_MODEL = "NO_ELIGIBLE_MODEL"
    MODEL_NOT_INSTALLED = "MODEL_NOT_INSTALLED"
    MODEL_CAPABILITY_MISMATCH = "MODEL_CAPABILITY_MISMATCH"
    PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"
    PROVIDER_CHANGED = "PROVIDER_CHANGED"
    ENDPOINT_CHANGED = "ENDPOINT_CHANGED"
    ROUTING_CONFIGURATION_CHANGED = "ROUTING_CONFIGURATION_CHANGED"
    ROUTE_PROVENANCE_MISMATCH = "ROUTE_PROVENANCE_MISMATCH"
    STAGE_ASSIGNMENT_CONFLICT = "STAGE_ASSIGNMENT_CONFLICT"
    FALLBACK_EXHAUSTED = "FALLBACK_EXHAUSTED"
    MODEL_UNAVAILABLE_DURING_STAGE = "MODEL_UNAVAILABLE_DURING_STAGE"
    MODEL_CAPABILITY_UNAVAILABLE = "MODEL_CAPABILITY_UNAVAILABLE"
    MODEL_DISCOVERY_TIMEOUT = "MODEL_DISCOVERY_TIMEOUT"
    CANCELLED = "CANCELLED"
    RESOURCE_LIMIT = "RESOURCE_LIMIT"


_MODEL_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,511}\Z")
_CONFIG_SIZE_LIMIT = 64 * 1024
_MAX_CANDIDATES = 32
_DISCOVERY_TIMEOUT = 20.0


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def _model_name(value: object, label: str = "model name") -> None:
    if (
        not isinstance(value, str) or not _MODEL_NAME.fullmatch(value)
        or "*" in value or ".." in value or "//" in value
    ):
        raise ValueError(f"Invalid {label}")


def endpoint_fingerprint(endpoint: str) -> str:
    if not isinstance(endpoint, str) or len(endpoint) > 2048:
        raise ValueError("Invalid provider endpoint")
    parts = urlsplit(endpoint)
    if parts.scheme not in {"http", "https"} or not parts.netloc or parts.username or parts.password:
        raise ValueError("Provider endpoint must be an absolute HTTP(S) URL without credentials")
    normalized = urlunsplit((
        parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip("/"),
        parts.query, "",
    ))
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def provider_identity(provider: ModelProvider) -> str:
    identity = type(provider).__name__[:128]
    if not identity:
        raise ValueError("Provider identity is unavailable")
    return identity


def session_fingerprint(session_id: str) -> str:
    if not isinstance(session_id, str) or not session_id or len(session_id) > 128:
        raise ValueError("Invalid conversation session identity")
    return hashlib.sha256(session_id.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ModelProfile:
    name: str
    enabled: bool = True
    roles: tuple[ModelRole, ...] = ()
    capability_tier: PreferenceTier = PreferenceTier.UNKNOWN
    resource_tier: PreferenceTier = PreferenceTier.UNKNOWN
    latency_tier: PreferenceTier = PreferenceTier.UNKNOWN
    context_capacity: int | None = None
    priority: int = 0

    def validate(self) -> None:
        _model_name(self.name)
        if type(self.enabled) is not bool:
            raise ValueError("Model profile enabled must be a boolean")
        if (
            not isinstance(self.roles, tuple) or len(self.roles) > len(ModelRole)
            or any(not isinstance(role, ModelRole) for role in self.roles)
            or len(set(self.roles)) != len(self.roles)
        ):
            raise ValueError("Invalid model profile roles")
        if any(not isinstance(tier, PreferenceTier) for tier in (
            self.capability_tier, self.resource_tier, self.latency_tier,
        )):
            raise ValueError("Invalid model profile tier")
        if self.context_capacity is not None and (
            type(self.context_capacity) is not int or not 256 <= self.context_capacity <= 10_000_000
        ):
            raise ValueError("Model context capacity must be a bounded, user-declared token capacity")
        if type(self.priority) is not int or not -1000 <= self.priority <= 1000:
            raise ValueError("Model profile priority must be between -1000 and 1000")

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return {
            "name": self.name,
            "enabled": self.enabled,
            "roles": [role.value for role in self.roles],
            "capability_tier": self.capability_tier.value,
            "resource_tier": self.resource_tier.value,
            "latency_tier": self.latency_tier.value,
            "context_capacity": self.context_capacity,
            "priority": self.priority,
        }

    @classmethod
    def from_dict(cls, value: object) -> ModelProfile:
        keys = {
            "name", "enabled", "roles", "capability_tier", "resource_tier", "latency_tier",
            "context_capacity", "priority",
        }
        if not isinstance(value, dict) or set(value) != keys or not isinstance(value["roles"], list):
            raise ValueError("Invalid model profile fields")
        try:
            profile = cls(
                name=value["name"],
                enabled=value["enabled"],
                roles=tuple(ModelRole(item) for item in value["roles"]),
                capability_tier=PreferenceTier(value["capability_tier"]),
                resource_tier=PreferenceTier(value["resource_tier"]),
                latency_tier=PreferenceTier(value["latency_tier"]),
                context_capacity=value["context_capacity"],
                priority=value["priority"],
            )
            profile.validate()
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Invalid model profile: {exc}") from exc
        return profile


@dataclass(frozen=True)
class RoleCandidates:
    role: ModelRole
    preferred: tuple[str, ...] = ()
    fallbacks: tuple[str, ...] = ()

    def validate(self) -> None:
        if not isinstance(self.role, ModelRole):
            raise ValueError("Invalid model role")
        for values, label in ((self.preferred, "preferred"), (self.fallbacks, "fallback")):
            if not isinstance(values, tuple) or len(values) > _MAX_CANDIDATES:
                raise ValueError(f"Invalid {label} model list")
            for name in values:
                _model_name(name)
            if len(set(values)) != len(values):
                raise ValueError(f"Duplicate {label} models")
        if set(self.preferred) & set(self.fallbacks):
            raise ValueError("A model cannot be both preferred and fallback for a role")

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return {"role": self.role.value, "preferred": list(self.preferred), "fallbacks": list(self.fallbacks)}

    @classmethod
    def from_dict(cls, value: object) -> RoleCandidates:
        if (
            not isinstance(value, dict)
            or set(value) != {"role", "preferred", "fallbacks"}
            or not isinstance(value["preferred"], list)
            or not isinstance(value["fallbacks"], list)
        ):
            raise ValueError("Invalid role candidate fields")
        try:
            result = cls(
                ModelRole(value["role"]), tuple(value["preferred"]), tuple(value["fallbacks"]),
            )
            result.validate()
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Invalid role candidates: {exc}") from exc
        return result


@dataclass(frozen=True)
class RoutingConfig:
    version: int = 1
    enabled: bool = False
    mode: RoutingMode = RoutingMode.SINGLE_MODEL
    strategy: RoutingStrategy = RoutingStrategy.BALANCED
    profiles: tuple[ModelProfile, ...] = ()
    roles: tuple[RoleCandidates, ...] = ()
    fallback_enabled: bool = True
    resource_preference: str = "balanced"
    complexity_enabled: bool = True

    def validate(self) -> None:
        if type(self.version) is not int or self.version != 1:
            raise ValueError("Unsupported model routing configuration version")
        if type(self.enabled) is not bool or type(self.fallback_enabled) is not bool:
            raise ValueError("Routing flags must be booleans")
        if type(self.complexity_enabled) is not bool:
            raise ValueError("Complexity routing flag must be a boolean")
        if not isinstance(self.mode, RoutingMode) or not isinstance(self.strategy, RoutingStrategy):
            raise ValueError("Invalid routing mode or strategy")
        if self.enabled != (self.mode == RoutingMode.ROUTED):
            raise ValueError("ROUTED mode must be explicitly enabled; SINGLE_MODEL must remain disabled")
        if self.resource_preference not in {
            "balanced", "lower_resource", "higher_capability", "lower_latency",
        }:
            raise ValueError("Unsupported resource preference")
        if (
            not isinstance(self.profiles, tuple) or len(self.profiles) > _MAX_CANDIDATES
            or any(not isinstance(item, ModelProfile) for item in self.profiles)
        ):
            raise ValueError("Invalid or excessive model profiles")
        for profile in self.profiles:
            profile.validate()
        profile_names = [profile.name for profile in self.profiles]
        if len(profile_names) != len(set(profile_names)):
            raise ValueError("Duplicate model profile definitions")
        if (
            not isinstance(self.roles, tuple) or len(self.roles) > len(ModelRole)
            or any(not isinstance(item, RoleCandidates) for item in self.roles)
        ):
            raise ValueError("Invalid routing role configuration")
        for role in self.roles:
            role.validate()
        role_names = [item.role for item in self.roles]
        if len(role_names) != len(set(role_names)):
            raise ValueError("Duplicate routing role definitions")
        referenced = {
            model
            for role in self.roles
            for model in (*role.preferred, *role.fallbacks)
        }
        if len(referenced) > _MAX_CANDIDATES:
            raise ValueError("Routing candidate pool exceeds the configured bound")
        payload = json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))
        if len(payload.encode("utf-8")) > _CONFIG_SIZE_LIMIT:
            raise ValueError("Model routing configuration exceeds 64 KiB")

    def fingerprint(self) -> str:
        self.validate()
        encoded = json.dumps(self.to_dict(), ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("ascii")).hexdigest()

    def role_candidates(self, role: ModelRole) -> RoleCandidates:
        return next((item for item in self.roles if item.role == role), RoleCandidates(role))

    def profile(self, name: str) -> ModelProfile | None:
        return next((item for item in self.profiles if item.name == name), None)

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "enabled": self.enabled,
            "mode": self.mode.value,
            "strategy": self.strategy.value,
            "profiles": [item.to_dict() for item in self.profiles],
            "roles": [item.to_dict() for item in self.roles],
            "fallback_enabled": self.fallback_enabled,
            "resource_preference": self.resource_preference,
            "complexity_enabled": self.complexity_enabled,
        }

    @classmethod
    def from_dict(cls, value: object) -> RoutingConfig:
        keys = {
            "version", "enabled", "mode", "strategy", "profiles", "roles",
            "fallback_enabled", "resource_preference", "complexity_enabled",
        }
        if (
            not isinstance(value, dict) or set(value) != keys
            or not isinstance(value["profiles"], list) or not isinstance(value["roles"], list)
        ):
            raise ValueError("Invalid model routing configuration fields")
        try:
            result = cls(
                version=value["version"],
                enabled=value["enabled"],
                mode=RoutingMode(value["mode"]),
                strategy=RoutingStrategy(value["strategy"]),
                profiles=tuple(ModelProfile.from_dict(item) for item in value["profiles"]),
                roles=tuple(RoleCandidates.from_dict(item) for item in value["roles"]),
                fallback_enabled=value["fallback_enabled"],
                resource_preference=value["resource_preference"],
                complexity_enabled=value["complexity_enabled"],
            )
            result.validate()
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Invalid model routing configuration: {exc}") from exc
        return result


@dataclass(frozen=True)
class ComplexityEstimate:
    tier: ComplexityTier
    reason: str

    def validate(self) -> None:
        if not isinstance(self.tier, ComplexityTier) or not isinstance(self.reason, str) or len(self.reason) > 512:
            raise ValueError("Invalid complexity estimate")

    def to_dict(self) -> dict[str, str]:
        self.validate()
        return {"tier": self.tier.value, "reason": self.reason}

    @classmethod
    def from_dict(cls, value: object) -> ComplexityEstimate:
        if not isinstance(value, dict) or set(value) != {"tier", "reason"}:
            raise ValueError("Invalid complexity estimate fields")
        try:
            result = cls(ComplexityTier(value["tier"]), value["reason"])
            result.validate()
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Invalid complexity estimate: {exc}") from exc
        return result


@dataclass(frozen=True)
class RoutingDecision:
    task_id: str
    role: ModelRole
    stage_id: str
    selected_model: str
    provider_identity: str
    endpoint_fingerprint: str
    required_capabilities: tuple[str, ...]
    validated_capabilities: tuple[str, ...]
    strategy: RoutingStrategy
    candidate_count: int
    reason_code: str
    configuration_fingerprint: str
    model_available: bool
    fallback_used: bool
    complexity: ComplexityEstimate
    created_at: str = field(default_factory=_timestamp)
    decision_id: str = field(default_factory=lambda: uuid4().hex)
    context_capacity_tokens: int | None = None
    context_capacity_status: str = "unknown"
    candidate_rejections: tuple[str, ...] = ()

    def validate(self) -> None:
        for value, label, maximum in (
            (self.task_id, "task ID", 128), (self.stage_id, "stage ID", 128),
            (self.provider_identity, "provider identity", 128),
            (self.reason_code, "routing reason", 128), (self.decision_id, "decision ID", 128),
        ):
            if not isinstance(value, str) or not value or len(value) > maximum:
                raise ValueError(f"Invalid routing {label}")
        _model_name(self.selected_model)
        for digest, label in (
            (self.endpoint_fingerprint, "endpoint fingerprint"),
            (self.configuration_fingerprint, "configuration fingerprint"),
        ):
            if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
                raise ValueError(f"Invalid routing {label}")
        if not isinstance(self.role, ModelRole) or not isinstance(self.strategy, RoutingStrategy):
            raise ValueError("Invalid routing decision role or strategy")
        for values, label in (
            (self.required_capabilities, "required capabilities"),
            (self.validated_capabilities, "validated capabilities"),
        ):
            if (
                not isinstance(values, tuple) or len(values) > 8
                or any(item not in {"chat", "native_tools"} for item in values)
                or len(set(values)) != len(values)
            ):
                raise ValueError(f"Invalid {label}")
        missing_capabilities = set(self.required_capabilities) - set(self.validated_capabilities)
        if missing_capabilities and not (
            missing_capabilities == {"chat"}
            and self.reason_code == "conversation_model_preserved"
        ):
            raise ValueError("Selected model lacks a required validated capability")
        if not isinstance(self.candidate_rejections, tuple) or len(self.candidate_rejections) > _MAX_CANDIDATES:
            raise ValueError("Invalid routing candidate limitations")
        seen_rejections: set[str] = set()
        rejection_codes = {
            "not_installed", "role_restricted", "model_unavailable",
            "capability_lookup_failed", "capability_metadata_invalid",
            "chat_capability_unknown", "chat_capability_unknown_legacy_preserved",
            "chat_unsupported", "native_tools_unsupported",
        }
        for item in self.candidate_rejections:
            if not isinstance(item, str) or len(item) > 576 or item in seen_rejections:
                raise ValueError("Invalid routing candidate limitation")
            name, separator, reason = item.partition("=")
            if not separator or reason not in rejection_codes:
                raise ValueError("Invalid routing candidate limitation")
            _model_name(name, "routing candidate limitation model")
            seen_rejections.add(item)
        if type(self.candidate_count) is not int or not 1 <= self.candidate_count <= _MAX_CANDIDATES:
            raise ValueError("Invalid routing candidate count")
        if type(self.model_available) is not bool or type(self.fallback_used) is not bool:
            raise ValueError("Invalid route availability or fallback flag")
        if not self.model_available:
            raise ValueError("A committed stage assignment must identify an available model")
        if self.context_capacity_tokens is not None and (
            type(self.context_capacity_tokens) is not int
            or not 256 <= self.context_capacity_tokens <= 10_000_000
        ):
            raise ValueError("Invalid declared context capacity")
        expected_context_status = (
            "user_declared_unverified"
            if self.context_capacity_tokens is not None else "unknown"
        )
        if self.context_capacity_status != expected_context_status:
            raise ValueError("Context capacity status must disclose its unverified nature")
        self.complexity.validate()
        if not isinstance(self.created_at, str):
            raise ValueError("Invalid routing timestamp")
        try:
            if datetime.fromisoformat(self.created_at).tzinfo is None:
                raise ValueError("Invalid routing timestamp")
        except ValueError as exc:
            raise ValueError("Invalid routing timestamp") from exc

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return {
            "task_id": self.task_id, "role": self.role.value, "stage_id": self.stage_id,
            "selected_model": self.selected_model, "provider_identity": self.provider_identity,
            "endpoint_fingerprint": self.endpoint_fingerprint,
            "required_capabilities": list(self.required_capabilities),
            "validated_capabilities": list(self.validated_capabilities),
            "strategy": self.strategy.value, "candidate_count": self.candidate_count,
            "reason_code": self.reason_code,
            "configuration_fingerprint": self.configuration_fingerprint,
            "model_available": self.model_available, "fallback_used": self.fallback_used,
            "complexity": self.complexity.to_dict(), "created_at": self.created_at,
            "decision_id": self.decision_id,
            "context_capacity_tokens": self.context_capacity_tokens,
            "context_capacity_status": self.context_capacity_status,
            "candidate_rejections": list(self.candidate_rejections),
        }

    @classmethod
    def from_dict(cls, value: object) -> RoutingDecision:
        keys = {
            "task_id", "role", "stage_id", "selected_model", "provider_identity",
            "endpoint_fingerprint", "required_capabilities", "validated_capabilities",
            "strategy", "candidate_count", "reason_code", "configuration_fingerprint",
            "model_available", "fallback_used", "complexity", "created_at", "decision_id",
            "context_capacity_tokens", "context_capacity_status",
        }
        current_keys = keys | {"candidate_rejections"}
        if (
            not isinstance(value, dict)
            or frozenset(value) not in {frozenset(keys), frozenset(current_keys)}
            or not isinstance(value["required_capabilities"], list)
            or not isinstance(value["validated_capabilities"], list)
            or "candidate_rejections" in value
            and not isinstance(value["candidate_rejections"], list)
        ):
            raise ValueError("Invalid routing decision fields")
        try:
            result = cls(
                task_id=value["task_id"], role=ModelRole(value["role"]),
                stage_id=value["stage_id"], selected_model=value["selected_model"],
                provider_identity=value["provider_identity"],
                endpoint_fingerprint=value["endpoint_fingerprint"],
                required_capabilities=tuple(value["required_capabilities"]),
                validated_capabilities=tuple(value["validated_capabilities"]),
                strategy=RoutingStrategy(value["strategy"]),
                candidate_count=value["candidate_count"], reason_code=value["reason_code"],
                configuration_fingerprint=value["configuration_fingerprint"],
                model_available=value["model_available"], fallback_used=value["fallback_used"],
                complexity=ComplexityEstimate.from_dict(value["complexity"]),
                created_at=value["created_at"], decision_id=value["decision_id"],
                context_capacity_tokens=value["context_capacity_tokens"],
                context_capacity_status=value["context_capacity_status"],
                candidate_rejections=tuple(value.get("candidate_rejections", [])),
            )
            result.validate()
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Invalid routing decision: {exc}") from exc
        return result


@dataclass
class TaskRouting:
    mode: RoutingMode
    configuration_fingerprint: str
    provider_identity: str
    endpoint_fingerprint: str
    session_fingerprint: str
    decisions: list[RoutingDecision] = field(default_factory=list)

    def validate(self, task_id: str) -> None:
        if not isinstance(self.mode, RoutingMode):
            raise ValueError("Invalid task routing mode")
        for digest in (
            self.configuration_fingerprint, self.endpoint_fingerprint,
            self.session_fingerprint,
        ):
            if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
                raise ValueError("Invalid task routing fingerprint")
        if not isinstance(self.provider_identity, str) or not self.provider_identity or len(self.provider_identity) > 128:
            raise ValueError("Invalid task routing provider")
        if not isinstance(self.decisions, list) or len(self.decisions) > 32:
            raise ValueError("Invalid task routing decisions")
        seen: set[tuple[ModelRole, str]] = set()
        for decision in self.decisions:
            if not isinstance(decision, RoutingDecision):
                raise ValueError("Invalid task routing decision")
            decision.validate()
            key = (decision.role, decision.stage_id)
            if decision.task_id != task_id or key in seen:
                raise ValueError("Routing decision belongs to another task or duplicates a stage")
            if (
                decision.provider_identity != self.provider_identity
                or decision.endpoint_fingerprint != self.endpoint_fingerprint
                or decision.configuration_fingerprint != self.configuration_fingerprint
            ):
                raise ValueError("Routing decision provenance differs from its task record")
            seen.add(key)

    def assignment(self, role: ModelRole, stage_id: str) -> RoutingDecision | None:
        return next(
            (item for item in self.decisions if item.role == role and item.stage_id == stage_id),
            None,
        )

    def append(self, decision: RoutingDecision, task_id: str) -> None:
        self.validate(task_id)
        if self.assignment(decision.role, decision.stage_id) is not None:
            existing = self.assignment(decision.role, decision.stage_id)
            if existing != decision:
                raise RoutingFailure(
                    RoutingErrorCode.STAGE_ASSIGNMENT_CONFLICT,
                    "A stage assignment is immutable once selected.",
                )
            return
        self.decisions.append(decision)
        self.validate(task_id)

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode.value,
            "configuration_fingerprint": self.configuration_fingerprint,
            "provider_identity": self.provider_identity,
            "endpoint_fingerprint": self.endpoint_fingerprint,
            "session_fingerprint": self.session_fingerprint,
            "decisions": [item.to_dict() for item in self.decisions],
        }

    @classmethod
    def from_dict(cls, value: object, task_id: str) -> TaskRouting:
        keys = {
            "mode", "configuration_fingerprint", "provider_identity",
            "endpoint_fingerprint", "session_fingerprint", "decisions",
        }
        if not isinstance(value, dict) or set(value) != keys or not isinstance(value["decisions"], list):
            raise ValueError("Invalid task routing fields")
        try:
            result = cls(
                mode=RoutingMode(value["mode"]),
                configuration_fingerprint=value["configuration_fingerprint"],
                provider_identity=value["provider_identity"],
                endpoint_fingerprint=value["endpoint_fingerprint"],
                session_fingerprint=value["session_fingerprint"],
                decisions=[RoutingDecision.from_dict(item) for item in value["decisions"]],
            )
            result.validate(task_id)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Invalid task routing: {exc}") from exc
        return result


class RoutingFailure(Exception):
    def __init__(self, code: RoutingErrorCode, message: str) -> None:
        super().__init__(message[:2048])
        self.code = code


def estimate_complexity(
    goal: str,
    *,
    context: Any = None,
    plan: Any = None,
    repair_attempts: int = 0,
    changed_files: int = 0,
) -> ComplexityEstimate:
    if not isinstance(goal, str) or not goal:
        return ComplexityEstimate(ComplexityTier.UNKNOWN, "No bounded task text was available.")
    signals: list[str] = []
    score = 0
    length = len(goal)
    if length >= 1200:
        score += 2
        signals.append("long task text")
    elif length >= 400:
        score += 1
        signals.append("moderate task text")
    if context is not None:
        items = len(getattr(context, "items", ()))
        if items >= 12:
            score += 2
            signals.append("many selected context items")
        elif items >= 5:
            score += 1
            signals.append("several selected context items")
        if getattr(context, "truncated", False):
            score += 1
            signals.append("truncated context")
    if plan is not None:
        steps = len(getattr(plan, "steps", ()))
        if steps >= 8:
            score += 2
            signals.append("many validated plan steps")
        elif steps >= 4:
            score += 1
            signals.append("multiple validated plan steps")
    if repair_attempts >= 2 or changed_files >= 8:
        score += 1
        signals.append("substantial recorded repair/change history")
    tier = (
        ComplexityTier.LOW if score <= 1 else
        ComplexityTier.MEDIUM if score <= 3 else
        ComplexityTier.HIGH
    )
    return ComplexityEstimate(
        tier,
        ("Signals: " + ", ".join(signals)) if signals else "Short task with limited structured evidence.",
    )


class ModelRouter:
    """Deterministic role routing over the existing provider connection."""

    @staticmethod
    async def _discover(
        operation: Awaitable[Any],
        cancellation: threading.Event | None,
        deadline: float | None = None,
    ) -> Any:
        pending = asyncio.ensure_future(operation)
        deadline = deadline or asyncio.get_running_loop().time() + _DISCOVERY_TIMEOUT
        try:
            while not pending.done():
                if cancellation and cancellation.is_set():
                    pending.cancel()
                    await asyncio.gather(pending, return_exceptions=True)
                    raise RoutingFailure(RoutingErrorCode.CANCELLED, "Model discovery was cancelled.")
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    pending.cancel()
                    await asyncio.gather(pending, return_exceptions=True)
                    raise TimeoutError("Model discovery exceeded its bounded duration.")
                await asyncio.wait({pending}, timeout=min(0.05, remaining))
            return pending.result()
        except BaseException:
            if not pending.done():
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
            raise

    async def validate_live_model(
        self,
        provider: ModelProvider,
        model: str,
        required_capabilities: tuple[str, ...],
        *,
        allow_unknown_chat: bool = False,
        cancellation: threading.Event | None = None,
    ) -> Any:
        """Refresh model availability and required capability evidence at a stage boundary."""
        try:
            _model_name(model)
        except ValueError as exc:
            raise RoutingFailure(RoutingErrorCode.ROUTE_PROVENANCE_MISMATCH, str(exc)) from exc
        if (
            not isinstance(required_capabilities, tuple)
            or any(item not in {"chat", "native_tools"} for item in required_capabilities)
            or len(set(required_capabilities)) != len(required_capabilities)
            or type(allow_unknown_chat) is not bool
        ):
            raise RoutingFailure(
                RoutingErrorCode.INVALID_ROUTING_CONFIGURATION,
                "Invalid stage capability requirement.",
            )
        if cancellation and cancellation.is_set():
            raise RoutingFailure(RoutingErrorCode.CANCELLED, "Stage model validation was cancelled.")

        deadline = asyncio.get_running_loop().time() + _DISCOVERY_TIMEOUT
        try:
            inventory = await self._discover(provider.list_models(), cancellation, deadline)
        except TimeoutError as exc:
            raise RoutingFailure(
                RoutingErrorCode.MODEL_DISCOVERY_TIMEOUT,
                "Stage model availability validation timed out.",
            ) from exc
        except RoutingFailure:
            raise
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise RoutingFailure(
                RoutingErrorCode.PROVIDER_UNAVAILABLE,
                f"Configured provider is unavailable: {str(exc)[:1024]}",
            ) from exc
        if cancellation and cancellation.is_set():
            raise RoutingFailure(RoutingErrorCode.CANCELLED, "Stage model validation was cancelled.")
        if not isinstance(inventory, list) or any(
            not isinstance(getattr(item, "name", None), str) for item in inventory
        ):
            raise RoutingFailure(
                RoutingErrorCode.PROVIDER_UNAVAILABLE,
                "Provider returned invalid model inventory.",
            )
        if model not in {item.name for item in inventory}:
            raise RoutingFailure(
                RoutingErrorCode.MODEL_UNAVAILABLE_DURING_STAGE,
                "The locked stage model is no longer installed.",
            )
        try:
            info = await self._discover(
                provider.capabilities(model), cancellation, deadline,
            )
        except TimeoutError as exc:
            raise RoutingFailure(
                RoutingErrorCode.MODEL_DISCOVERY_TIMEOUT,
                "Stage capability validation timed out.",
            ) from exc
        except RoutingFailure:
            raise
        except asyncio.CancelledError:
            raise
        except ModelUnavailableError as exc:
            raise RoutingFailure(
                RoutingErrorCode.MODEL_UNAVAILABLE_DURING_STAGE,
                "The locked stage model is no longer available.",
            ) from exc
        except ModelCapabilityMetadataError as exc:
            raise RoutingFailure(
                RoutingErrorCode.MODEL_CAPABILITY_UNAVAILABLE,
                "The locked stage model returned malformed capability metadata.",
            ) from exc
        except Exception as exc:
            raise RoutingFailure(
                RoutingErrorCode.PROVIDER_UNAVAILABLE,
                f"Configured provider capability check failed: {str(exc)[:1024]}",
            ) from exc

        chat = getattr(info, "chat", None)
        if (
            getattr(info, "name", None) != model
            or type(getattr(info, "tools", None)) is not bool
            or type(getattr(info, "thinking", None)) is not bool
            or chat is not None and type(chat) is not bool
            or getattr(info, "capability_error", None) is not None
            and not isinstance(getattr(info, "capability_error", None), str)
            or getattr(info, "capability_error", None)
        ):
            raise RoutingFailure(
                RoutingErrorCode.MODEL_CAPABILITY_UNAVAILABLE,
                "The locked stage model returned invalid capability metadata.",
            )
        if chat is False or chat is None and not allow_unknown_chat:
            raise RoutingFailure(
                RoutingErrorCode.MODEL_CAPABILITY_UNAVAILABLE,
                "The locked stage model lacks verified conversational-generation capability.",
            )
        if "native_tools" in required_capabilities and not info.tools:
            raise RoutingFailure(
                RoutingErrorCode.MODEL_CAPABILITY_UNAVAILABLE,
                "The locked stage model no longer advertises native-tool capability.",
            )
        return info

    async def select(
        self,
        provider: ModelProvider,
        config: RoutingConfig,
        *,
        task_id: str,
        role: ModelRole,
        stage_id: str,
        requested_model: str,
        endpoint: str,
        complexity: ComplexityEstimate,
        require_tools: bool,
        cancellation: threading.Event | None = None,
    ) -> RoutingDecision:
        try:
            config.validate()
            complexity.validate()
            _model_name(requested_model)
            if not isinstance(role, ModelRole) or not isinstance(task_id, str) or not task_id:
                raise ValueError("Invalid role or task identity")
            if not isinstance(stage_id, str) or not stage_id or len(stage_id) > 128:
                raise ValueError("Invalid stage identifier")
            if type(require_tools) is not bool:
                raise ValueError("Tool requirement must be a boolean")
            endpoint_hash = endpoint_fingerprint(endpoint)
            provider_name = provider_identity(provider)
            actual_endpoint = getattr(provider, "base_url", endpoint)
            if endpoint_fingerprint(actual_endpoint) != endpoint_hash:
                raise RoutingFailure(
                    RoutingErrorCode.ENDPOINT_CHANGED,
                    "Provider endpoint differs from the authorized conversation endpoint.",
                )
        except RoutingFailure:
            raise
        except (TypeError, ValueError) as exc:
            raise RoutingFailure(RoutingErrorCode.INVALID_ROUTING_CONFIGURATION, str(exc)) from exc
        if cancellation and cancellation.is_set():
            raise RoutingFailure(RoutingErrorCode.CANCELLED, "Model routing was cancelled.")

        config_hash = config.fingerprint()
        discovery_deadline = asyncio.get_running_loop().time() + _DISCOVERY_TIMEOUT
        required = ("chat", "native_tools") if require_tools else ("chat",)
        if config.mode == RoutingMode.SINGLE_MODEL:
            candidate_groups = ((requested_model,), ())
        else:
            preferences = config.role_candidates(role)
            profile_names = tuple(
                profile.name for profile in config.profiles
                if profile.enabled and (not profile.roles or role in profile.roles)
            )
            primary = tuple(dict.fromkeys(
                preferences.preferred if preferences.preferred else profile_names
            ))
            if config.strategy == RoutingStrategy.PINNED:
                primary = primary[:1]
            candidate_groups = (primary, preferences.fallbacks if config.fallback_enabled else ())

        candidates = tuple(dict.fromkeys((*candidate_groups[0], *candidate_groups[1])))
        if not candidates:
            raise RoutingFailure(
                RoutingErrorCode.NO_ELIGIBLE_MODEL,
                f"No configured model candidate exists for {role.value}.",
            )
        if len(candidates) > _MAX_CANDIDATES:
            raise RoutingFailure(RoutingErrorCode.RESOURCE_LIMIT, "Routing candidate count exceeds its bound.")

        try:
            installed = await self._discover(
                provider.list_models(), cancellation, discovery_deadline,
            )
        except TimeoutError as exc:
            raise RoutingFailure(RoutingErrorCode.MODEL_DISCOVERY_TIMEOUT, "Model discovery timed out.") from exc
        except RoutingFailure:
            raise
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise RoutingFailure(
                RoutingErrorCode.PROVIDER_UNAVAILABLE,
                f"Configured provider is unavailable: {str(exc)[:1024]}",
            ) from exc
        if cancellation and cancellation.is_set():
            raise RoutingFailure(RoutingErrorCode.CANCELLED, "Model routing was cancelled.")
        installed_names: set[str] = set()
        for item in installed:
            if not isinstance(getattr(item, "name", None), str):
                raise RoutingFailure(RoutingErrorCode.PROVIDER_UNAVAILABLE, "Provider returned invalid model inventory.")
            installed_names.add(item.name)

        eligible: dict[str, ModelInfo] = {}
        candidate_rejections: list[str] = []
        unavailable: set[str] = set()
        restricted: set[str] = set()
        capability_failures: set[str] = set()
        completed_capability_lookups = 0

        async def inspect_group(names: tuple[str, ...], *, first_only: bool = False) -> None:
            nonlocal completed_capability_lookups
            for name in names:
                if cancellation and cancellation.is_set():
                    raise RoutingFailure(RoutingErrorCode.CANCELLED, "Model routing was cancelled.")
                if name not in installed_names:
                    unavailable.add(name)
                    candidate_rejections.append(f"{name}=not_installed")
                    continue
                profile = config.profile(name)
                if profile is not None and (
                    not profile.enabled or profile.roles and role not in profile.roles
                ):
                    restricted.add(name)
                    candidate_rejections.append(f"{name}=role_restricted")
                    continue
                try:
                    info = await self._discover(
                        provider.capabilities(name), cancellation, discovery_deadline,
                    )
                except TimeoutError as exc:
                    raise RoutingFailure(
                        RoutingErrorCode.MODEL_DISCOVERY_TIMEOUT,
                        f"Capability discovery timed out for {name}.",
                    ) from exc
                except RoutingFailure:
                    raise
                except asyncio.CancelledError:
                    raise
                except ModelUnavailableError:
                    unavailable.add(name)
                    candidate_rejections.append(f"{name}=model_unavailable")
                    continue
                except ModelCapabilityMetadataError:
                    completed_capability_lookups += 1
                    candidate_rejections.append(f"{name}=capability_metadata_invalid")
                    continue
                except Exception:
                    capability_failures.add(name)
                    candidate_rejections.append(f"{name}=capability_lookup_failed")
                    continue
                completed_capability_lookups += 1
                chat = getattr(info, "chat", None)
                if (
                    getattr(info, "name", None) != name
                    or type(getattr(info, "tools", None)) is not bool
                    or type(getattr(info, "thinking", None)) is not bool
                    or chat is not None and type(chat) is not bool
                    or getattr(info, "capability_error", None) is not None
                    and not isinstance(getattr(info, "capability_error", None), str)
                ):
                    candidate_rejections.append(f"{name}=capability_metadata_invalid")
                    continue
                if info.capability_error:
                    candidate_rejections.append(f"{name}=capability_metadata_invalid")
                    continue
                if chat is False:
                    candidate_rejections.append(f"{name}=chat_unsupported")
                    continue
                if chat is None and config.mode != RoutingMode.SINGLE_MODEL:
                    candidate_rejections.append(f"{name}=chat_capability_unknown")
                    continue
                if require_tools and not info.tools:
                    candidate_rejections.append(f"{name}=native_tools_unsupported")
                    continue
                eligible[name] = info
                if chat is None:
                    candidate_rejections.append(
                        f"{name}=chat_capability_unknown_legacy_preserved",
                    )
                if first_only:
                    break

        primary_names = candidate_groups[0]
        if config.strategy == RoutingStrategy.PINNED:
            primary_names = primary_names[:1]
        await inspect_group(primary_names)
        primary_eligible = [name for name in primary_names if name in eligible]
        fallback_names = candidate_groups[1]
        if not primary_eligible and fallback_names:
            await inspect_group(
                fallback_names,
                first_only=config.strategy == RoutingStrategy.PINNED,
            )
        fallback_eligible = [name for name in fallback_names if name in eligible]
        used_fallback = not primary_eligible
        pool = fallback_eligible if used_fallback else primary_eligible
        if not pool:
            if capability_failures and completed_capability_lookups == 0:
                raise RoutingFailure(
                    RoutingErrorCode.PROVIDER_UNAVAILABLE,
                    "Capability discovery failed for every available routing candidate.",
                )
            code = (
                RoutingErrorCode.MODEL_NOT_INSTALLED
                if all(name in unavailable for name in candidates)
                else RoutingErrorCode.MODEL_CAPABILITY_MISMATCH
            )
            if candidate_groups[1] and used_fallback:
                code = RoutingErrorCode.FALLBACK_EXHAUSTED
            details = (
                "No configured candidate is installed."
                if code == RoutingErrorCode.MODEL_NOT_INSTALLED
                else (
                    "No configured candidate is enabled and role-eligible with all required capabilities."
                    if restricted else "No installed candidate advertises all required capabilities."
                )
            )
            raise RoutingFailure(code, details)

        if config.mode == RoutingMode.SINGLE_MODEL:
            selected = pool[0]
            reason = "conversation_model_preserved"
        elif config.strategy == RoutingStrategy.PINNED:
            selected = pool[0]
            reason = "pinned_model" if not used_fallback else "configured_fallback"
        else:
            selected = self._rank(pool, config, role, complexity)[0]
            reason = (
                "highest_configured_eligible_priority"
                if config.strategy == RoutingStrategy.CAPABILITY_FIRST
                else "role_requirements_and_declared_preferences"
            )
            if used_fallback:
                reason = "configured_fallback"
        validated_items = []
        if eligible[selected].chat is True:
            validated_items.append("chat")
        if eligible[selected].tools:
            validated_items.append("native_tools")
        validated = tuple(validated_items)
        decision = RoutingDecision(
            task_id=task_id,
            role=role,
            stage_id=stage_id,
            selected_model=selected,
            provider_identity=provider_name,
            endpoint_fingerprint=endpoint_hash,
            required_capabilities=required,
            validated_capabilities=validated,
            strategy=config.strategy,
            candidate_count=len(candidates),
            reason_code=reason,
            configuration_fingerprint=config_hash,
            model_available=True,
            fallback_used=used_fallback,
            complexity=complexity,
            context_capacity_tokens=(
                config.profile(selected).context_capacity
                if config.profile(selected) is not None else None
            ),
            context_capacity_status=(
                "user_declared_unverified"
                if config.profile(selected) is not None
                and config.profile(selected).context_capacity is not None
                else "unknown"
            ),
            candidate_rejections=tuple(candidate_rejections),
        )
        decision.validate()
        return decision

    @staticmethod
    def _rank(
        candidates: list[str],
        config: RoutingConfig,
        role: ModelRole,
        complexity: ComplexityEstimate,
    ) -> list[str]:
        preference = config.role_candidates(role).preferred

        def rank(name: str) -> tuple[int, int, int, int, int, str]:
            profile = config.profile(name)
            if profile is None:
                capability = PreferenceTier.UNKNOWN
                resource = PreferenceTier.UNKNOWN
                priority = 0
            else:
                capability = profile.capability_tier
                resource = profile.resource_tier
                priority = profile.priority
            tier_rank = {
                PreferenceTier.UNKNOWN: 0, PreferenceTier.SMALL: 1,
                PreferenceTier.MEDIUM: 2, PreferenceTier.LARGE: 3,
            }[capability]
            if config.resource_preference == "higher_capability":
                capability_preference = tier_rank
            elif complexity.tier == ComplexityTier.LOW and config.strategy == RoutingStrategy.BALANCED:
                capability_preference = {
                    PreferenceTier.SMALL: 3, PreferenceTier.MEDIUM: 2,
                    PreferenceTier.UNKNOWN: 1, PreferenceTier.LARGE: 0,
                }[capability]
            elif config.strategy == RoutingStrategy.CAPABILITY_FIRST or complexity.tier in {
                ComplexityTier.HIGH, ComplexityTier.UNKNOWN,
            }:
                capability_preference = tier_rank
            else:
                capability_preference = {
                    PreferenceTier.MEDIUM: 3, PreferenceTier.SMALL: 2,
                    PreferenceTier.UNKNOWN: 1, PreferenceTier.LARGE: 1,
                }[capability]
            resource_rank = {
                PreferenceTier.UNKNOWN: 0, PreferenceTier.SMALL: 3,
                PreferenceTier.MEDIUM: 2, PreferenceTier.LARGE: 1,
            }[resource]
            latency = profile.latency_tier if profile else PreferenceTier.UNKNOWN
            latency_rank = {
                PreferenceTier.UNKNOWN: 0, PreferenceTier.SMALL: 3,
                PreferenceTier.MEDIUM: 2, PreferenceTier.LARGE: 1,
            }[latency]
            role_preference = len(preference) - preference.index(name) if name in preference else 0
            if config.strategy == RoutingStrategy.BALANCED:
                if config.resource_preference == "lower_resource":
                    return resource_rank, capability_preference, priority, role_preference, tier_rank, name
                if config.resource_preference == "lower_latency":
                    return latency_rank, capability_preference, priority, role_preference, tier_rank, name
                if config.resource_preference == "higher_capability":
                    return tier_rank, priority, resource_rank, role_preference, capability_preference, name
            return capability_preference, priority, role_preference, resource_rank, tier_rank, name

        return sorted(candidates, key=rank, reverse=True)


def explain_routing(decision: RoutingDecision) -> str:
    decision.validate()
    required = ", ".join(decision.required_capabilities)
    context = (
        f"user-declared {decision.context_capacity_tokens} tokens (fit unverified)"
        if decision.context_capacity_tokens is not None else "unknown; exact token fit is unverified"
    )
    return "\n".join((
        f"Role: {decision.role.value.upper()}",
        f"Selected model: {decision.selected_model}",
        f"Reason: {decision.reason_code}",
        f"Strategy: {decision.strategy.value.upper()}",
        f"Complexity: {decision.complexity.tier.value.upper()}",
        f"Alternative candidates: {max(0, decision.candidate_count - 1)}",
        f"Required capabilities: {required}",
        f"Fallback used: {'Yes' if decision.fallback_used else 'No'}",
        f"Context capacity: {context}",
    ))


__all__ = [
    "ComplexityEstimate", "ComplexityTier", "ModelProfile", "ModelRole", "ModelRouter",
    "PreferenceTier", "RoleCandidates", "RoutingConfig", "RoutingDecision", "RoutingErrorCode",
    "RoutingFailure", "RoutingMode", "RoutingStrategy", "TaskRouting",
    "endpoint_fingerprint", "estimate_complexity", "provider_identity",
    "explain_routing", "session_fingerprint",
]
