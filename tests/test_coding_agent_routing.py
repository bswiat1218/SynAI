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
from synai.coding_agent.runtime import CodingAgentRuntime
from synai.config import ConversationEnvironment, Settings
from synai.intelligence import RepositoryIndex
from synai.models import ChatEvent, ModelInfo, Session
from synai.preferences import Preferences, PreferencesStore
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
        self.implementation_calls = 0

    async def list_models(self) -> list[ModelInfo]:
        return [ModelInfo(name) for name in sorted(self.models)]

    async def capabilities(self, name: str) -> ModelInfo:
        if name not in self.models:
            raise AssertionError(f"Unexpected capabilities request for {name}")
        return ModelInfo(name, tools=self.models[name])

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
            result = await runtime.run_task(
                task, GOAL, session, RepositoryIndex(root), model=session.model,
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
            restored = AgentTask.from_dict(task.to_dict())
            self.assertEqual(restored.routing, task.routing)

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
            session.session_id = "different-session"
            with self.assertRaises(RoutingFailure):
                runtime._validate_route_context(task, session)

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
            result = await runtime.run_task(
                task, GOAL, session, RepositoryIndex(root), model=session.model,
            )
            self.assertTrue(result.ok, result.error)
            decision = task.routing.assignment(ModelRole.IMPLEMENTATION, "implementation")
            self.assertEqual(decision.selected_model, "coder")
            self.assertTrue(decision.fallback_used)
            self.assertNotIn("coder-small", [call[0] for call in provider.requests])
            self.assertEqual(task.executions[0].model, "coder")


if __name__ == "__main__":
    unittest.main()
