from __future__ import annotations

import asyncio
import json
import threading
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import patch

import synai.coding_agent.routing as routing_module
from synai.coding_agent import (
    AgentStatus,
    AgentTask,
    ComplexityTier,
    ContextEngine,
    ModelProfile,
    ModelRole,
    ModelRouter,
    PreferenceTier,
    RoleCandidates,
    RoutingConfig,
    RoutingErrorCode,
    RoutingFailure,
    RoutingMode,
    RoutingStrategy,
    TaskRouting,
    estimate_complexity,
    explain_routing,
    session_fingerprint,
)
from synai.coding_agent.runtime import CodingAgentRuntime, RuntimeErrorCode, _RuntimeStop
from synai.config import ConversationEnvironment, Settings
from synai.intelligence import RepositoryIndex
from synai.models import ChatEvent, ModelInfo, Session
from synai.preferences import Preferences, PreferencesStore
from synai.providers.base import ProviderError
from synai.tools import Tools

from test_coding_agent_runtime import FakeBackend


ENDPOINT = "http://localhost:11434"
GOAL = "Read the existing client implementation."


def routed_config(
    strategy: RoutingStrategy = RoutingStrategy.BALANCED,
    *,
    fallbacks: tuple[str, ...] = (),
) -> RoutingConfig:
    return RoutingConfig(
        enabled=True,
        mode=RoutingMode.ROUTED,
        strategy=strategy,
        profiles=(
            ModelProfile(
                "planner", roles=(ModelRole.PLANNING,),
                capability_tier=PreferenceTier.SMALL, resource_tier=PreferenceTier.SMALL,
                priority=20,
            ),
            ModelProfile(
                "coder", roles=(ModelRole.IMPLEMENTATION, ModelRole.REPAIR),
                capability_tier=PreferenceTier.LARGE, resource_tier=PreferenceTier.LARGE,
                priority=20,
            ),
            ModelProfile(
                "coder-small", roles=(ModelRole.IMPLEMENTATION, ModelRole.REPAIR),
                capability_tier=PreferenceTier.SMALL, resource_tier=PreferenceTier.SMALL,
                priority=10,
            ),
        ),
        roles=(
            RoleCandidates(ModelRole.PLANNING, ("planner",)),
            RoleCandidates(
                ModelRole.IMPLEMENTATION, ("coder-small",), fallbacks or ("coder",),
            ),
            RoleCandidates(ModelRole.REPAIR, ("coder", "coder-small")),
            RoleCandidates(ModelRole.REVIEW, ("planner",)),
        ),
    )


class RoutingProvider:
    base_url = ENDPOINT

    def __init__(self, models: dict[str, bool] | None = None) -> None:
        self.models = models or {"planner": False, "coder": True, "coder-small": True}
        self.requests: list[tuple[str, list[Any], list[dict[str, Any]]]] = []
        self.capability_requests: list[str] = []
        self.implementation_calls = 0

    async def list_models(self) -> list[ModelInfo]:
        return [ModelInfo(name) for name in sorted(self.models)]

    async def capabilities(self, name: str) -> ModelInfo:
        if name not in self.models:
            raise AssertionError(f"Unexpected capabilities request for {name}")
        self.capability_requests.append(name)
        return ModelInfo(name, tools=self.models[name], chat=True)

    async def chat(self, model: str, messages: list[Any], tools: list[dict[str, Any]]):
        self.requests.append((model, list(messages), list(tools)))
        if model == "planner":
            payload = {
                "goal": GOAL,
                "assumptions": [],
                "uncertainties": [],
                "completion_criteria": [],
                "verification_intent": [],
                "steps": [{
                    "id": "step-1",
                    "description": "Read the client implementation",
                    "purpose": "Use current source as evidence",
                    "depends_on": [],
                    "paths": ["app/client.py"],
                    "symbols": [],
                    "operations": ["read"],
                    "expected_outcome": "The current source is available",
                    "verification_criteria": [],
                    "verification_intents": [],
                }],
            }
            yield ChatEvent(content=json.dumps(payload), done=True)
            return
        self.implementation_calls += 1
        if self.implementation_calls == 1:
            yield ChatEvent(tool_calls=[{
                "function": {
                    "name": "read_file",
                    "arguments": {"path": "app/client.py"},
                },
            }], done=True)
            return
        yield ChatEvent(content="Read complete.", done=True)


class RoutingEngineTests(unittest.IsolatedAsyncioTestCase):
    async def test_single_model_mode_preserves_exact_requested_model(self) -> None:
        provider = RoutingProvider({"legacy": True, "replacement": True})
        decision = await ModelRouter().select(
            provider, RoutingConfig(), task_id="task", role=ModelRole.IMPLEMENTATION,
            stage_id="implementation", requested_model="legacy", endpoint=ENDPOINT,
            complexity=estimate_complexity("small task"), require_tools=True,
        )
        self.assertEqual(decision.selected_model, "legacy")
        self.assertFalse(decision.fallback_used)
        self.assertEqual(decision.reason_code, "conversation_model_preserved")

    async def test_pinned_uses_only_exact_primary_or_explicit_fallback(self) -> None:
        config = RoutingConfig(
            enabled=True,
            mode=RoutingMode.ROUTED,
            strategy=RoutingStrategy.PINNED,
            roles=(RoleCandidates(ModelRole.IMPLEMENTATION, ("missing",), ("coder",)),),
        )
        decision = await ModelRouter().select(
            RoutingProvider({"coder": True}), config, task_id="task",
            role=ModelRole.IMPLEMENTATION, stage_id="implementation",
            requested_model="ignored", endpoint=ENDPOINT,
            complexity=estimate_complexity("task"), require_tools=True,
        )
        self.assertEqual(decision.selected_model, "coder")
        self.assertTrue(decision.fallback_used)

    async def test_persisted_stage_assignment_is_immutable(self) -> None:
        config = RoutingConfig()
        session_id = "conversation"
        decision = await ModelRouter().select(
            RoutingProvider({"legacy": True}), config, task_id="task",
            role=ModelRole.IMPLEMENTATION, stage_id="implementation",
            requested_model="legacy", endpoint=ENDPOINT,
            complexity=estimate_complexity("task"), require_tools=True,
        )
        routing = TaskRouting(
            RoutingMode.SINGLE_MODEL,
            config.fingerprint(),
            decision.provider_identity,
            decision.endpoint_fingerprint,
            session_fingerprint(session_id),
        )
        routing.append(decision, "task")
        conflicting = replace(decision, selected_model="replacement")
        with self.assertRaises(RoutingFailure) as raised:
            routing.append(conflicting, "task")
        self.assertEqual(raised.exception.code, RoutingErrorCode.STAGE_ASSIGNMENT_CONFLICT)

    async def test_incompatible_primary_is_rejected_before_tool_stage(self) -> None:
        provider = RoutingProvider({"text-only": False, "tool-model": True})
        config = RoutingConfig(
            enabled=True,
            mode=RoutingMode.ROUTED,
            roles=(RoleCandidates(ModelRole.IMPLEMENTATION, ("text-only",), ("tool-model",)),),
        )
        decision = await ModelRouter().select(
            provider, config, task_id="task", role=ModelRole.IMPLEMENTATION,
            stage_id="implementation", requested_model="unused", endpoint=ENDPOINT,
            complexity=estimate_complexity("task"), require_tools=True,
        )
        self.assertEqual(decision.selected_model, "tool-model")
        self.assertTrue(decision.fallback_used)
        self.assertEqual(decision.validated_capabilities, ("chat", "native_tools"))

    async def test_candidate_capability_failure_does_not_invalidate_healthy_primary(self) -> None:
        class CandidateFailureProvider(RoutingProvider):
            async def capabilities(self, name: str) -> ModelInfo:
                if name == "broken":
                    raise OSError("candidate capability lookup failed")
                return await super().capabilities(name)

        config = RoutingConfig(
            enabled=True,
            mode=RoutingMode.ROUTED,
            roles=(RoleCandidates(
                ModelRole.PLANNING, ("healthy", "broken"), ("fallback",),
            ),),
        )
        provider = CandidateFailureProvider({
            "healthy": False, "broken": False, "fallback": False,
        })
        decision = await ModelRouter().select(
            provider, config, task_id="task", role=ModelRole.PLANNING,
            stage_id="planning", requested_model="unused", endpoint=ENDPOINT,
            complexity=estimate_complexity("task"), require_tools=False,
        )
        self.assertEqual(decision.selected_model, "healthy")
        self.assertFalse(decision.fallback_used)
        self.assertIn("broken=capability_lookup_failed", decision.candidate_rejections)
        self.assertNotIn("fallback", provider.capability_requests)

    async def test_healthy_primary_does_not_query_lower_priority_fallback(self) -> None:
        class BrokenFallbackProvider(RoutingProvider):
            async def capabilities(self, name: str) -> ModelInfo:
                self.capability_requests.append(name)
                if name == "broken-fallback":
                    raise OSError("fallback show request failed")
                return ModelInfo(name, chat=True)

        config = RoutingConfig(
            enabled=True,
            mode=RoutingMode.ROUTED,
            roles=(RoleCandidates(
                ModelRole.PLANNING, ("healthy-primary",), ("broken-fallback",),
            ),),
        )
        provider = BrokenFallbackProvider({
            "healthy-primary": False, "broken-fallback": False,
        })
        decision = await ModelRouter().select(
            provider, config, task_id="task", role=ModelRole.PLANNING,
            stage_id="planning", requested_model="unused", endpoint=ENDPOINT,
            complexity=estimate_complexity("task"), require_tools=False,
        )
        self.assertEqual(decision.selected_model, "healthy-primary")
        self.assertFalse(decision.fallback_used)
        self.assertEqual(provider.capability_requests, ["healthy-primary"])

    async def test_failed_primary_capability_lookup_uses_healthy_configured_fallback(self) -> None:
        class CandidateFailureProvider(RoutingProvider):
            async def capabilities(self, name: str) -> ModelInfo:
                if name == "broken":
                    raise OSError("primary capability lookup failed")
                return await super().capabilities(name)

        config = RoutingConfig(
            enabled=True,
            mode=RoutingMode.ROUTED,
            roles=(RoleCandidates(ModelRole.PLANNING, ("broken",), ("healthy",)),),
        )
        decision = await ModelRouter().select(
            CandidateFailureProvider({"broken": False, "healthy": False}),
            config, task_id="task", role=ModelRole.PLANNING, stage_id="planning",
            requested_model="unused", endpoint=ENDPOINT,
            complexity=estimate_complexity("task"), require_tools=False,
        )
        self.assertEqual(decision.selected_model, "healthy")
        self.assertTrue(decision.fallback_used)
        self.assertEqual(decision.reason_code, "configured_fallback")
        self.assertIn("broken=capability_lookup_failed", decision.candidate_rejections)

    async def test_missing_candidate_does_not_block_installed_candidate(self) -> None:
        config = RoutingConfig(
            enabled=True,
            mode=RoutingMode.ROUTED,
            roles=(RoleCandidates(ModelRole.PLANNING, ("missing", "installed")),),
        )
        decision = await ModelRouter().select(
            RoutingProvider({"installed": False}), config, task_id="task",
            role=ModelRole.PLANNING, stage_id="planning", requested_model="unused",
            endpoint=ENDPOINT, complexity=estimate_complexity("task"),
            require_tools=False,
        )
        self.assertEqual(decision.selected_model, "installed")
        self.assertIn("missing=not_installed", decision.candidate_rejections)

    async def test_malformed_candidate_metadata_does_not_block_fallback(self) -> None:
        class MalformedProvider(RoutingProvider):
            async def capabilities(self, name: str) -> ModelInfo:
                if name == "malformed":
                    return ModelInfo(
                        name, tools=True, capability_error="invalid capability metadata",
                    )
                return await super().capabilities(name)

        config = RoutingConfig(
            enabled=True,
            mode=RoutingMode.ROUTED,
            roles=(RoleCandidates(
                ModelRole.IMPLEMENTATION, ("malformed",), ("healthy",),
            ),),
        )
        decision = await ModelRouter().select(
            MalformedProvider({"malformed": True, "healthy": True}), config,
            task_id="task", role=ModelRole.IMPLEMENTATION, stage_id="implementation",
            requested_model="unused", endpoint=ENDPOINT,
            complexity=estimate_complexity("task"), require_tools=True,
        )
        self.assertEqual(decision.selected_model, "healthy")
        self.assertIn("malformed=capability_metadata_invalid", decision.candidate_rejections)

    async def test_all_candidate_capability_transport_failures_remain_provider_failure(self) -> None:
        class OfflineShowProvider(RoutingProvider):
            async def capabilities(self, name: str) -> ModelInfo:
                raise OSError(f"/api/show unavailable for {name}")

        config = RoutingConfig(
            enabled=True,
            mode=RoutingMode.ROUTED,
            roles=(RoleCandidates(ModelRole.PLANNING, ("one", "two")),),
        )
        with self.assertRaises(RoutingFailure) as raised:
            await ModelRouter().select(
                OfflineShowProvider({"one": False, "two": False}), config,
                task_id="task", role=ModelRole.PLANNING, stage_id="planning",
                requested_model="unused", endpoint=ENDPOINT,
                complexity=estimate_complexity("task"), require_tools=False,
            )
        self.assertEqual(raised.exception.code, RoutingErrorCode.PROVIDER_UNAVAILABLE)

    async def test_live_stage_validation_observes_deadline_and_cancellation(self) -> None:
        class SlowProvider(RoutingProvider):
            async def capabilities(self, name: str) -> ModelInfo:
                await asyncio.sleep(0.5)
                return await super().capabilities(name)

        router = ModelRouter()
        provider = SlowProvider({"planner": False})
        with patch.object(routing_module, "_DISCOVERY_TIMEOUT", 0.01):
            with self.assertRaises(RoutingFailure) as timed_out:
                await router.validate_live_model(provider, "planner", ("chat",))
        self.assertEqual(timed_out.exception.code, RoutingErrorCode.MODEL_DISCOVERY_TIMEOUT)

        cancellation = threading.Event()

        async def cancel_soon() -> None:
            await asyncio.sleep(0.01)
            cancellation.set()

        asyncio.create_task(cancel_soon())
        with self.assertRaises(RoutingFailure) as cancelled:
            await router.validate_live_model(
                provider, "planner", ("chat",), cancellation=cancellation,
            )
        self.assertEqual(cancelled.exception.code, RoutingErrorCode.CANCELLED)

    async def test_pinned_fallback_can_be_disabled(self) -> None:
        config = RoutingConfig(
            enabled=True,
            mode=RoutingMode.ROUTED,
            strategy=RoutingStrategy.PINNED,
            fallback_enabled=False,
            roles=(RoleCandidates(ModelRole.IMPLEMENTATION, ("missing",), ("coder",)),),
        )
        with self.assertRaises(RoutingFailure) as raised:
            await ModelRouter().select(
                RoutingProvider({"coder": True}), config, task_id="task",
                role=ModelRole.IMPLEMENTATION, stage_id="implementation",
                requested_model="ignored", endpoint=ENDPOINT,
                complexity=estimate_complexity("task"), require_tools=True,
            )
        self.assertEqual(raised.exception.code, RoutingErrorCode.MODEL_NOT_INSTALLED)

    async def test_candidate_rejection_keeps_balanced_and_capability_first_deterministic(self) -> None:
        class MixedProvider(RoutingProvider):
            async def capabilities(self, name: str) -> ModelInfo:
                if name == "broken":
                    raise OSError("bad candidate")
                return await super().capabilities(name)

        for strategy in (RoutingStrategy.BALANCED, RoutingStrategy.CAPABILITY_FIRST):
            with self.subTest(strategy=strategy):
                config = RoutingConfig(
                    enabled=True,
                    mode=RoutingMode.ROUTED,
                    strategy=strategy,
                    roles=(RoleCandidates(
                        ModelRole.PLANNING, ("broken", "healthy"),
                    ),),
                )
                provider = MixedProvider({"broken": False, "healthy": False})
                first = await ModelRouter().select(
                    provider, config, task_id="task", role=ModelRole.PLANNING,
                    stage_id="first", requested_model="unused", endpoint=ENDPOINT,
                    complexity=estimate_complexity("task"), require_tools=False,
                )
                second = await ModelRouter().select(
                    provider, config, task_id="task", role=ModelRole.PLANNING,
                    stage_id="second", requested_model="unused", endpoint=ENDPOINT,
                    complexity=estimate_complexity("task"), require_tools=False,
                )
                self.assertEqual(first.selected_model, "healthy")
                self.assertEqual(second.selected_model, "healthy")
                self.assertEqual(first.candidate_rejections, second.candidate_rejections)

    async def test_routed_roles_require_explicit_chat_capability(self) -> None:
        config = RoutingConfig(
            enabled=True,
            mode=RoutingMode.ROUTED,
            roles=(RoleCandidates(ModelRole.PLANNING, ("unknown", "chat")),),
        )
        class ChatMetadataProvider(RoutingProvider):
            async def capabilities(self, name: str) -> ModelInfo:
                if name == "unknown":
                    return ModelInfo(name, chat=None)
                return ModelInfo(name, chat=True)

        decision = await ModelRouter().select(
            ChatMetadataProvider({"unknown": False, "chat": False}), config,
            task_id="task", role=ModelRole.PLANNING, stage_id="planning",
            requested_model="unused", endpoint=ENDPOINT,
            complexity=estimate_complexity("task"), require_tools=False,
        )
        self.assertEqual(decision.selected_model, "chat")
        self.assertIn("unknown=chat_capability_unknown", decision.candidate_rejections)
        self.assertIn("chat", decision.validated_capabilities)

    async def test_planning_and_review_reject_embedding_only_candidates(self) -> None:
        class MixedInventoryProvider(RoutingProvider):
            async def capabilities(self, name: str) -> ModelInfo:
                if name == "embedding":
                    return ModelInfo(name, chat=False)
                return ModelInfo(name, chat=True)

        for role in (ModelRole.PLANNING, ModelRole.REVIEW):
            with self.subTest(role=role):
                config = RoutingConfig(
                    enabled=True,
                    mode=RoutingMode.ROUTED,
                    roles=(RoleCandidates(role, ("embedding", "conversation")),),
                )
                decision = await ModelRouter().select(
                    MixedInventoryProvider({"embedding": False, "conversation": False}),
                    config, task_id="task", role=role, stage_id=role.value,
                    requested_model="unused", endpoint=ENDPOINT,
                    complexity=estimate_complexity("task"), require_tools=False,
                )
                self.assertEqual(decision.selected_model, "conversation")
                self.assertIn("embedding=chat_unsupported", decision.candidate_rejections)
                self.assertIn("chat", decision.validated_capabilities)

    async def test_repair_role_requires_both_chat_and_native_tools(self) -> None:
        config = RoutingConfig(
            enabled=True,
            mode=RoutingMode.ROUTED,
            roles=(RoleCandidates(
                ModelRole.REPAIR, ("text-only", "tool-chat"),
            ),),
        )
        provider = RoutingProvider({"text-only": False, "tool-chat": True})
        decision = await ModelRouter().select(
            provider, config, task_id="task", role=ModelRole.REPAIR,
            stage_id="repair-1", requested_model="unused", endpoint=ENDPOINT,
            complexity=estimate_complexity("repair"), require_tools=True,
        )
        self.assertEqual(decision.selected_model, "tool-chat")
        self.assertEqual(decision.validated_capabilities, ("chat", "native_tools"))
        self.assertIn("text-only=native_tools_unsupported", decision.candidate_rejections)

    async def test_no_conversational_model_is_a_capability_failure(self) -> None:
        class EmbeddingOnlyProvider(RoutingProvider):
            async def capabilities(self, name: str) -> ModelInfo:
                return ModelInfo(name, chat=False)

        config = RoutingConfig(
            enabled=True,
            mode=RoutingMode.ROUTED,
            roles=(RoleCandidates(ModelRole.PLANNING, ("embedding",)),),
        )
        with self.assertRaises(RoutingFailure) as raised:
            await ModelRouter().select(
                EmbeddingOnlyProvider({"embedding": False}), config,
                task_id="task", role=ModelRole.PLANNING, stage_id="planning",
                requested_model="unused", endpoint=ENDPOINT,
                complexity=estimate_complexity("task"), require_tools=False,
            )
        self.assertEqual(raised.exception.code, RoutingErrorCode.MODEL_CAPABILITY_MISMATCH)

    async def test_single_model_preserves_unknown_chat_without_claiming_validation(self) -> None:
        class LegacyProvider(RoutingProvider):
            async def capabilities(self, name: str) -> ModelInfo:
                return ModelInfo(name, tools=False)

        decision = await ModelRouter().select(
            LegacyProvider({"legacy": False}), RoutingConfig(), task_id="task",
            role=ModelRole.PLANNING, stage_id="planning", requested_model="legacy",
            endpoint=ENDPOINT, complexity=estimate_complexity("task"),
            require_tools=False,
        )
        self.assertEqual(decision.selected_model, "legacy")
        self.assertNotIn("chat", decision.validated_capabilities)
        self.assertIn(
            "legacy=chat_capability_unknown_legacy_preserved",
            decision.candidate_rejections,
        )
        legacy_record = decision.to_dict()
        legacy_record.pop("candidate_rejections")
        restored = type(decision).from_dict(legacy_record)
        self.assertEqual(restored.candidate_rejections, ())
        self.assertNotIn("chat", restored.validated_capabilities)

    async def test_no_eligible_model_has_typed_capability_failure(self) -> None:
        config = RoutingConfig(
            enabled=True,
            mode=RoutingMode.ROUTED,
            roles=(RoleCandidates(ModelRole.IMPLEMENTATION, ("planner",)),),
        )
        with self.assertRaises(RoutingFailure) as raised:
            await ModelRouter().select(
                RoutingProvider({"planner": False}), config, task_id="task",
                role=ModelRole.IMPLEMENTATION, stage_id="implementation",
                requested_model="unused", endpoint=ENDPOINT,
                complexity=estimate_complexity("task"), require_tools=True,
            )
        self.assertEqual(raised.exception.code, RoutingErrorCode.MODEL_CAPABILITY_MISMATCH)

    async def test_balanced_strategy_uses_complexity_and_declared_tiers(self) -> None:
        config = RoutingConfig(
            enabled=True,
            mode=RoutingMode.ROUTED,
            strategy=RoutingStrategy.BALANCED,
            profiles=(
                ModelProfile("small", capability_tier=PreferenceTier.SMALL),
                ModelProfile("large", capability_tier=PreferenceTier.LARGE),
            ),
            roles=(RoleCandidates(ModelRole.PLANNING, ("large", "small")),),
        )
        provider = RoutingProvider({"small": False, "large": False})
        router = ModelRouter()
        low = await router.select(
            provider, config, task_id="task", role=ModelRole.PLANNING,
            stage_id="low", requested_model="unused", endpoint=ENDPOINT,
            complexity=estimate_complexity("short"), require_tools=False,
        )
        high = await router.select(
            provider, config, task_id="task", role=ModelRole.PLANNING,
            stage_id="high", requested_model="unused", endpoint=ENDPOINT,
            complexity=type(estimate_complexity("short"))(ComplexityTier.HIGH, "many validated steps"),
            require_tools=False,
        )
        self.assertEqual(low.selected_model, "small")
        self.assertEqual(high.selected_model, "large")
        self.assertIn("Complexity: HIGH", explain_routing(high))

    async def test_capability_first_prefers_highest_declared_tier(self) -> None:
        config = RoutingConfig(
            enabled=True,
            mode=RoutingMode.ROUTED,
            strategy=RoutingStrategy.CAPABILITY_FIRST,
            profiles=(
                ModelProfile(
                    "large", capability_tier=PreferenceTier.LARGE,
                ),
                ModelProfile(
                    "medium", capability_tier=PreferenceTier.MEDIUM,
                ),
            ),
            roles=(RoleCandidates(ModelRole.REVIEW, ("medium", "large")),),
        )
        decision = await ModelRouter().select(
            RoutingProvider({"large": False, "medium": False}), config,
            task_id="task", role=ModelRole.REVIEW, stage_id="review",
            requested_model="unused", endpoint=ENDPOINT,
            complexity=estimate_complexity("short"), require_tools=False,
        )
        self.assertEqual(decision.selected_model, "large")

    async def test_lower_resource_preference_uses_user_declared_tier(self) -> None:
        config = RoutingConfig(
            enabled=True,
            mode=RoutingMode.ROUTED,
            strategy=RoutingStrategy.BALANCED,
            resource_preference="lower_resource",
            profiles=(
                ModelProfile(
                    "light", capability_tier=PreferenceTier.SMALL,
                    resource_tier=PreferenceTier.SMALL,
                ),
                ModelProfile(
                    "heavy", capability_tier=PreferenceTier.LARGE,
                    resource_tier=PreferenceTier.LARGE,
                ),
            ),
            roles=(RoleCandidates(ModelRole.PLANNING, ("heavy", "light")),),
        )
        decision = await ModelRouter().select(
            RoutingProvider({"light": False, "heavy": False}), config,
            task_id="task", role=ModelRole.PLANNING, stage_id="planning",
            requested_model="unused", endpoint=ENDPOINT,
            complexity=estimate_complexity("short"), require_tools=False,
        )
        self.assertEqual(decision.selected_model, "light")

    async def test_higher_capability_preference_overrides_low_complexity_hint(self) -> None:
        config = RoutingConfig(
            enabled=True,
            mode=RoutingMode.ROUTED,
            strategy=RoutingStrategy.BALANCED,
            resource_preference="higher_capability",
            profiles=(
                ModelProfile("small", capability_tier=PreferenceTier.SMALL),
                ModelProfile("large", capability_tier=PreferenceTier.LARGE),
            ),
            roles=(RoleCandidates(ModelRole.PLANNING, ("small", "large")),),
        )
        decision = await ModelRouter().select(
            RoutingProvider({"small": False, "large": False}), config,
            task_id="task", role=ModelRole.PLANNING, stage_id="planning",
            requested_model="unused", endpoint=ENDPOINT,
            complexity=estimate_complexity("short"), require_tools=False,
        )
        self.assertEqual(decision.selected_model, "large")

    async def test_discovery_observes_cancellation_and_provider_failure(self) -> None:
        cancellation = threading.Event()

        class SlowProvider(RoutingProvider):
            async def list_models(self) -> list[ModelInfo]:
                await asyncio.sleep(0.5)
                return await super().list_models()

        async def cancel_soon() -> None:
            await asyncio.sleep(0.01)
            cancellation.set()

        asyncio.create_task(cancel_soon())
        with self.assertRaises(RoutingFailure) as raised:
            await ModelRouter().select(
                SlowProvider(), routed_config(), task_id="task",
                role=ModelRole.PLANNING, stage_id="planning",
                requested_model="unused", endpoint=ENDPOINT,
                complexity=estimate_complexity("task"), require_tools=False,
                cancellation=cancellation,
            )
        self.assertEqual(raised.exception.code, RoutingErrorCode.CANCELLED)

        cancellation.clear()

        class SlowCapabilityProvider(RoutingProvider):
            async def capabilities(self, name: str) -> ModelInfo:
                await asyncio.sleep(0.5)
                return await super().capabilities(name)

        asyncio.create_task(cancel_soon())
        with self.assertRaises(RoutingFailure) as raised:
            await ModelRouter().select(
                SlowCapabilityProvider(), routed_config(), task_id="task",
                role=ModelRole.PLANNING, stage_id="planning",
                requested_model="unused", endpoint=ENDPOINT,
                complexity=estimate_complexity("task"), require_tools=False,
                cancellation=cancellation,
            )
        self.assertEqual(raised.exception.code, RoutingErrorCode.CANCELLED)

        class OfflineProvider(RoutingProvider):
            async def list_models(self) -> list[ModelInfo]:
                raise OSError("server unavailable")

        with self.assertRaises(RoutingFailure) as raised:
            await ModelRouter().select(
                OfflineProvider(), routed_config(), task_id="task",
                role=ModelRole.PLANNING, stage_id="planning",
                requested_model="unused", endpoint=ENDPOINT,
                complexity=estimate_complexity("task"), require_tools=False,
            )
        self.assertEqual(raised.exception.code, RoutingErrorCode.PROVIDER_UNAVAILABLE)

    async def test_endpoint_mismatch_and_bad_configuration_fail_closed(self) -> None:
        with self.assertRaises(RoutingFailure) as raised:
            await ModelRouter().select(
                RoutingProvider(), routed_config(), task_id="task",
                role=ModelRole.PLANNING, stage_id="planning",
                requested_model="ignored", endpoint="http://elsewhere:11434",
                complexity=estimate_complexity("task"), require_tools=False,
            )
        self.assertEqual(raised.exception.code, RoutingErrorCode.ENDPOINT_CHANGED)
        with self.assertRaises(ValueError):
            RoutingConfig(
                enabled=True, mode=RoutingMode.ROUTED,
                roles=(RoleCandidates(ModelRole.PLANNING, ("*",)),),
            ).validate()

    async def test_model_discovery_has_one_aggregate_timeout(self) -> None:
        class SlowProvider(RoutingProvider):
            async def capabilities(self, name: str) -> ModelInfo:
                await asyncio.sleep(0.02)
                return await super().capabilities(name)

        config = RoutingConfig(
            enabled=True,
            mode=RoutingMode.ROUTED,
            profiles=(ModelProfile("first"), ModelProfile("second")),
            roles=(RoleCandidates(ModelRole.PLANNING, ("first", "second")),),
        )
        with patch.object(routing_module, "_DISCOVERY_TIMEOUT", 0.03):
            with self.assertRaises(RoutingFailure) as raised:
                await ModelRouter().select(
                    SlowProvider({"first": False, "second": False}), config,
                    task_id="task", role=ModelRole.PLANNING, stage_id="planning",
                    requested_model="unused", endpoint=ENDPOINT,
                    complexity=estimate_complexity("small task"), require_tools=False,
                )
        self.assertEqual(raised.exception.code, RoutingErrorCode.MODEL_DISCOVERY_TIMEOUT)

    async def test_configuration_profiles_and_legacy_preferences_roundtrip(self) -> None:
        config = routed_config()
        self.assertEqual(RoutingConfig.from_dict(config.to_dict()), config)
        with tempfile.TemporaryDirectory() as directory:
            store = PreferencesStore(Path(directory))
            store.save(Preferences(model_routing=config))
            self.assertEqual(store.load().model_routing, config)

    async def test_routed_task_records_distinct_planning_and_implementation_models(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "project"
            (root / "app").mkdir(parents=True)
            (root / "app" / "client.py").write_text("class Client: pass\n", encoding="utf-8")
            settings = replace(Settings(execution_mode="host"), history_dir=Path(directory) / ".synai")
            session = Session("conversation-model", ENDPOINT, str(root))
            session.set_environment(ConversationEnvironment.from_settings(settings, root))
            provider = RoutingProvider()
            backend = FakeBackend(root, settings)

            async def approval(_name: str, _description: str) -> bool:
                return True

            runtime = CodingAgentRuntime(
                provider,
                Tools(backend, approval),
                context_engine=ContextEngine(),
                routing_config=routed_config(),
            )
            task = AgentTask(GOAL)
            events = []

            async def observe(event) -> None:
                events.append(event)
                if event.kind == "model_route_selected":
                    raise RuntimeError("nonfatal observer failure")

            result = await runtime.run_task(
                task, GOAL, session, RepositoryIndex(root), model=session.model,
                event_sink=observe,
            )
            self.assertTrue(result.ok, result.error)
            self.assertEqual(task.status, AgentStatus.COMPLETED)
            self.assertEqual(task.selected_model, "conversation-model")
            self.assertEqual(task.plan.planner_model, "planner")
            self.assertEqual(task.executions[0].model, "coder-small")
            self.assertEqual(
                task.routing.assignment(ModelRole.PLANNING, "planning").selected_model,
                "planner",
            )
            self.assertEqual(
                task.routing.assignment(ModelRole.IMPLEMENTATION, "implementation").selected_model,
                "coder-small",
            )
            self.assertEqual(provider.requests[0][0], "planner")
            self.assertEqual(provider.requests[0][2], [])
            self.assertEqual(provider.requests[1][0], "coder-small")
            self.assertTrue(provider.requests[1][2])
            route_events = [event for event in events if event.kind == "model_route_selected"]
            self.assertEqual(
                [(event.model_role, event.selected_model, event.step_id) for event in route_events],
                [("planning", "planner", "planning"),
                 ("implementation", "coder-small", "implementation")],
            )
            self.assertTrue(all(event.task_id == task.task_id for event in route_events))
            self.assertTrue(all(event.routing_strategy == "balanced" for event in route_events))
            self.assertTrue(all(event.selection_reason for event in route_events))
            self.assertTrue(all(event.fallback_used is False for event in route_events))
            self.assertTrue(all(len(event.message or "") <= 4096 for event in route_events))
            restored = AgentTask.from_dict(task.to_dict())
            self.assertEqual(restored.routing, task.routing)

    async def test_all_role_assignments_emit_only_after_checkpoint_acceptance(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = replace(Settings(execution_mode="host"), history_dir=root / ".synai")
            session = Session("conversation-model", ENDPOINT, str(root))
            session.set_environment(ConversationEnvironment.from_settings(settings, root))
            config = routed_config()
            config = replace(
                config,
                profiles=(
                    replace(
                        config.profiles[0],
                        roles=(ModelRole.PLANNING, ModelRole.REVIEW),
                    ),
                    *config.profiles[1:],
                ),
            )
            runtime = CodingAgentRuntime(
                RoutingProvider(), Tools(FakeBackend(root, settings), None),
                routing_config=config,
            )
            task = AgentTask(GOAL)
            runtime._start_task_routing(task, session)
            observed = []

            async def sink(event) -> None:
                observed.append(event)

            async def fail_checkpoint(_checkpoint) -> None:
                raise OSError("checkpoint unavailable")

            for role, stage_id, needs_tools in (
                (ModelRole.PLANNING, "planning", False),
                (ModelRole.IMPLEMENTATION, "implementation", True),
                (ModelRole.REPAIR, "repair-1", True),
                (ModelRole.REVIEW, "review-run", False),
            ):
                await runtime._assign_stage(
                    task, session, role, stage_id, session.model,
                    estimate_complexity("task"), require_tools=needs_tools,
                    cancellation=None, checkpoint=None, event_sink=sink,
                )
            self.assertEqual([event.model_role for event in observed], [
                "planning", "implementation", "repair", "review",
            ])
            for event in observed:
                assignment = task.routing.assignment(ModelRole(event.model_role), event.step_id)
                self.assertIsNotNone(assignment)
                self.assertEqual(event.selected_model, assignment.selected_model)
                self.assertEqual(event.routing_strategy, assignment.strategy.value)
                self.assertEqual(event.selection_reason, assignment.reason_code)

            failed_task = AgentTask(GOAL)
            runtime._start_task_routing(failed_task, session)
            with self.assertRaises(OSError):
                await runtime._assign_stage(
                    failed_task, session, ModelRole.PLANNING, "planning", session.model,
                    estimate_complexity("task"), require_tools=False,
                    cancellation=None, checkpoint=fail_checkpoint, event_sink=sink,
                )
            self.assertEqual(len(observed), 4)

    async def test_routed_assignment_is_revalidated_before_stage_execution(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "project"
            (root / "app").mkdir(parents=True)
            (root / "app" / "client.py").write_text("class Client: pass\n", encoding="utf-8")
            settings = replace(Settings(execution_mode="host"), history_dir=Path(directory) / ".synai")
            session = Session("conversation-model", ENDPOINT, str(root))
            session.set_environment(ConversationEnvironment.from_settings(settings, root))
            provider = RoutingProvider()
            backend = FakeBackend(root, settings)

            async def approval(_name: str, _description: str) -> bool:
                return True

            runtime = CodingAgentRuntime(
                provider, Tools(backend, approval), context_engine=ContextEngine(),
                routing_config=routed_config(),
            )
            task = AgentTask(GOAL)
            removed = False

            async def remove_before_implementation(checkpoint) -> None:
                nonlocal removed
                session.agent_checkpoint = checkpoint
                decision = task.routing.assignment(
                    ModelRole.IMPLEMENTATION, "implementation",
                )
                if decision is not None and not removed:
                    provider.models.pop(decision.selected_model)
                    removed = True

            result = await runtime.run_task(
                task, GOAL, session, RepositoryIndex(root), model=session.model,
                checkpoint=remove_before_implementation,
            )
            self.assertFalse(result.ok)
            self.assertEqual(result.error.code.value, "model_routing_error")
            self.assertIn("MODEL_UNAVAILABLE_DURING_STAGE", result.error.message)
            self.assertEqual([request[0] for request in provider.requests], ["planner"])
            self.assertEqual(backend.calls, [])
            self.assertEqual(
                task.routing.assignment(
                    ModelRole.IMPLEMENTATION, "implementation",
                ).selected_model,
                "coder-small",
            )
            self.assertTrue(removed)

    async def test_capability_change_before_stage_start_fails_without_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = replace(Settings(execution_mode="host"), history_dir=root / ".synai")
            session = Session("conversation-model", ENDPOINT, str(root))
            session.set_environment(ConversationEnvironment.from_settings(settings, root))
            provider = RoutingProvider()
            runtime = CodingAgentRuntime(
                provider, Tools(FakeBackend(root, settings), None),
                routing_config=routed_config(),
            )
            task = AgentTask(GOAL)
            runtime._start_task_routing(task, session)
            decision = await runtime._assign_stage(
                task, session, ModelRole.IMPLEMENTATION, "implementation",
                session.model, estimate_complexity("task"),
                require_tools=True, cancellation=None, checkpoint=None,
            )
            provider.models[decision.selected_model] = False
            with self.assertRaises(_RuntimeStop) as raised:
                await runtime._validate_provider_for_execution(
                    decision.selected_model, task, session=session,
                    role=ModelRole.IMPLEMENTATION, stage_id="implementation",
                )
            self.assertEqual(raised.exception.code, RuntimeErrorCode.MODEL_ROUTING_ERROR)
            self.assertIn("MODEL_CAPABILITY_UNAVAILABLE", raised.exception.message)
            self.assertEqual(
                task.routing.assignment(
                    ModelRole.IMPLEMENTATION, "implementation",
                ).selected_model,
                decision.selected_model,
            )

    async def test_repair_and_review_assignments_are_live_checked_at_stage_start(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = replace(Settings(execution_mode="host"), history_dir=root / ".synai")
            session = Session("conversation-model", ENDPOINT, str(root))
            session.set_environment(ConversationEnvironment.from_settings(settings, root))
            config = routed_config()
            config = replace(
                config,
                profiles=(
                    replace(
                        config.profiles[0],
                        roles=(ModelRole.PLANNING, ModelRole.REVIEW),
                    ),
                    *config.profiles[1:],
                ),
            )
            provider = RoutingProvider()
            runtime = CodingAgentRuntime(
                provider, Tools(FakeBackend(root, settings), None),
                routing_config=config,
            )
            task = AgentTask(GOAL)
            runtime._start_task_routing(task, session)
            for role, stage_id in (
                (ModelRole.REPAIR, "repair-1"),
                (ModelRole.REVIEW, "review-run"),
            ):
                decision = await runtime._assign_stage(
                    task, session, role, stage_id, session.model,
                    estimate_complexity("task"),
                    require_tools=role == ModelRole.REPAIR,
                    cancellation=None, checkpoint=None,
                )
                provider.models.pop(decision.selected_model)
                with self.assertRaises(_RuntimeStop) as raised:
                    await runtime._validate_provider_for_execution(
                        decision.selected_model, task, session=session,
                        role=role, stage_id=stage_id,
                    )
                self.assertEqual(raised.exception.code, RuntimeErrorCode.MODEL_ROUTING_ERROR)
                self.assertIn(
                    "MODEL_UNAVAILABLE_DURING_STAGE", raised.exception.message,
                )
                provider.models[decision.selected_model] = role in {
                    ModelRole.REPAIR, ModelRole.IMPLEMENTATION,
                }

    async def test_model_loss_after_mutation_does_not_fallback_or_replay(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "project"
            (root / "app").mkdir(parents=True)
            target = root / "app" / "client.py"
            target.write_text("class Client: pass\n", encoding="utf-8")
            settings = replace(Settings(execution_mode="host"), history_dir=Path(directory) / ".synai")
            session = Session("conversation-model", ENDPOINT, str(root))
            session.set_environment(ConversationEnvironment.from_settings(settings, root))

            class MutationProvider(RoutingProvider):
                async def chat(self, model: str, messages, tools):
                    self.requests.append((model, list(messages), list(tools)))
                    if model == "planner":
                        payload = {
                            "goal": GOAL,
                            "assumptions": [],
                            "uncertainties": [],
                            "completion_criteria": ["The client source is updated"],
                            "verification_intent": ["targeted_tests"],
                            "steps": [{
                                "id": "step-1",
                                "description": "Update the client",
                                "purpose": "Apply the planned source change",
                                "depends_on": [],
                                "paths": ["app/client.py"],
                                "symbols": [],
                                "operations": ["modify"],
                                "expected_outcome": "The source contains the updated class",
                                "verification_criteria": ["The source change is present"],
                                "verification_intents": [],
                            }],
                        }
                        yield ChatEvent(content=json.dumps(payload), done=True)
                        return
                    if model != "coder-small" or model not in self.models:
                        raise ProviderError("Selected implementation model is unavailable")
                    self.implementation_calls += 1
                    if self.implementation_calls != 1:
                        raise ProviderError("Selected implementation model is unavailable")
                    yield ChatEvent(tool_calls=[{
                        "function": {
                            "name": "write_file",
                            "arguments": {
                                "path": "app/client.py",
                                "content": "class Client:\n    pass\n",
                            },
                        },
                    }], done=True)

            provider = MutationProvider({"planner": False, "coder": True, "coder-small": True})

            class RemoveAfterMutationBackend(FakeBackend):
                async def execute(self, name, arguments, expected_sha256=None):
                    result = await super().execute(name, arguments, expected_sha256)
                    if name == "write_file" and result.get("ok"):
                        provider.models.pop("coder-small", None)
                    return result

            backend = RemoveAfterMutationBackend(root, settings)

            async def approval(_name: str, _description: str) -> bool:
                return True

            runtime = CodingAgentRuntime(
                provider, Tools(backend, approval), context_engine=ContextEngine(),
                routing_config=routed_config(fallbacks=("coder",)),
            )
            task = AgentTask(GOAL)

            async def sink(event) -> None:
                if event.kind == "model_route_selected":
                    route_events.append(event)

            route_events = []
            result = await runtime.run_task(
                task, GOAL, session, RepositoryIndex(root), model=session.model,
                event_sink=sink,
            )
            self.assertFalse(result.ok)
            self.assertEqual(result.error.code.value, "model_error")
            self.assertEqual(target.read_text(encoding="utf-8"), "class Client:\n    pass\n")
            self.assertEqual(
                sum(call[0] == "write_file" for call in backend.calls),
                1,
            )
            self.assertEqual(
                [execution.status.value for execution in task.executions],
                ["succeeded"],
            )
            implementation_calls = [
                request for request in provider.requests if request[0] != "planner"
            ]
            self.assertEqual([request[0] for request in implementation_calls], ["coder-small", "coder-small"])
            self.assertEqual(
                task.routing.assignment(
                    ModelRole.IMPLEMENTATION, "implementation",
                ).selected_model,
                "coder-small",
            )

    async def test_routing_context_rejects_a_changed_conversation_session(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "project"
            root.mkdir()
            settings = replace(Settings(execution_mode="host"), history_dir=Path(directory) / ".synai")
            session = Session("conversation-model", ENDPOINT, str(root))
            session.set_environment(ConversationEnvironment.from_settings(settings, root))
            runtime = CodingAgentRuntime(
                RoutingProvider(), Tools(FakeBackend(root, settings), None),
                routing_config=routed_config(),
            )
            task = AgentTask(GOAL)
            runtime._start_task_routing(task, session)
            original_session_id = session.session_id
            session.session_id = "different-session"
            with self.assertRaises(RoutingFailure):
                runtime._validate_route_context(task, session)
            session.session_id = original_session_id
            original_config = runtime.routing_config
            runtime.routing_config = replace(original_config, fallback_enabled=False)
            with self.assertRaises(RoutingFailure) as changed_config:
                runtime._validate_route_context(task, session)
            self.assertEqual(
                changed_config.exception.code,
                RoutingErrorCode.ROUTING_CONFIGURATION_CHANGED,
            )
            runtime.routing_config = original_config
            session.endpoint = "http://elsewhere:11434"
            with self.assertRaises(RoutingFailure) as changed_endpoint:
                runtime._validate_route_context(task, session)
            self.assertEqual(
                changed_endpoint.exception.code,
                RoutingErrorCode.ENDPOINT_CHANGED,
            )

    async def test_incompatible_implementation_falls_back_before_any_tool_request(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "project"
            (root / "app").mkdir(parents=True)
            (root / "app" / "client.py").write_text("class Client: pass\n", encoding="utf-8")
            settings = replace(Settings(execution_mode="host"), history_dir=Path(directory) / ".synai")
            session = Session("conversation-model", ENDPOINT, str(root))
            session.set_environment(ConversationEnvironment.from_settings(settings, root))
            provider = RoutingProvider({"planner": False, "coder": True, "coder-small": False})
            backend = FakeBackend(root, settings)

            async def approval(_name: str, _description: str) -> bool:
                return True

            runtime = CodingAgentRuntime(
                provider, Tools(backend, approval), context_engine=ContextEngine(),
                routing_config=routed_config(fallbacks=("coder",)),
            )
            task = AgentTask(GOAL)
            route_events = []

            async def sink(event) -> None:
                if event.kind == "model_route_selected":
                    route_events.append(event)

            result = await runtime.run_task(
                task, GOAL, session, RepositoryIndex(root), model=session.model,
                event_sink=sink,
            )
            self.assertTrue(result.ok, result.error)
            decision = task.routing.assignment(ModelRole.IMPLEMENTATION, "implementation")
            self.assertEqual(decision.selected_model, "coder")
            self.assertTrue(decision.fallback_used)
            implementation_event = next(
                event for event in route_events if event.model_role == "implementation"
            )
            self.assertEqual(implementation_event.selected_model, "coder")
            self.assertTrue(implementation_event.fallback_used)
            self.assertEqual(implementation_event.selection_reason, "configured_fallback")
            self.assertNotIn("coder-small", [call[0] for call in provider.requests])
            self.assertEqual(task.executions[0].model, "coder")


if __name__ == "__main__":
    unittest.main()
