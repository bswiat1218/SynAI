from __future__ import annotations

import asyncio
import json
import tempfile
import threading
import unittest
from dataclasses import replace
from pathlib import Path

from synai.coding_agent import (
    AgentCheckpoint,
    AgentPlan,
    AgentStatus,
    AgentTask,
    ContextEngine,
    ContextRequest,
    PlanOperation,
    Planner,
    PlannerLimits,
    PlanningErrorCode,
    PlanningRequest,
    PlanningWorkspace,
    VerificationIntent,
    attach_validated_plan,
    render_plan,
)
from synai.config import ConversationEnvironment, Settings
from synai.history import History
from synai.models import ChatEvent, Message, Session
from synai.providers.base import ProviderError
from synai.intelligence import RepositoryIndex


def make_plan(
    *,
    paths: list[str] | None = None,
    symbols: list[str] | None = None,
    operations: list[str] | None = None,
    verification_criteria: list[str] | None = None,
    verification_intent: list[str] | None = None,
    required_outputs: list[str] | None = None,
) -> dict:
    return {
        "goal": "Add bounded retry handling to Client.request",
        "assumptions": ["Retries apply only to transient failures"],
        "uncertainties": [],
        "completion_criteria": ["Retries are bounded", "Existing request behavior remains compatible"],
        "verification_intent": (
            verification_intent if verification_intent is not None else ["targeted_tests"]
        ),
        "steps": [{
            "id": "step-1",
            "description": "Implement bounded retry handling",
            "purpose": "Retry transient request failures without changing the public API",
            "depends_on": [],
            "paths": paths if paths is not None else ["app/client.py"],
            "symbols": symbols if symbols is not None else ["app.client.Client.request"],
            "operations": operations if operations is not None else ["modify"],
            "expected_outcome": "Transient failures are retried a bounded number of times",
            "verification_criteria": (
                verification_criteria if verification_criteria is not None
                else ["Focused retry tests pass"]
            ),
            "verification_intents": [],
            **({"required_outputs": required_outputs} if required_outputs is not None else {}),
        }],
    }


class FakeProvider:
    def __init__(self, outputs: list[str] | None = None, *, failure: Exception | None = None) -> None:
        self.outputs = list(outputs or [])
        self.failure = failure
        self.requests: list[tuple[str, list[Message], list[dict]]] = []

    async def list_models(self):
        return []

    async def capabilities(self, name: str):
        return name

    async def chat(self, model: str, messages: list[Message], tools: list[dict]):
        self.requests.append((model, messages, tools))
        if self.failure is not None:
            raise self.failure
        if not self.outputs:
            raise AssertionError("No fake planner output remains")
        output = self.outputs.pop(0)
        yield ChatEvent(content=output, done=True)


class PlannerFixture(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / "app").mkdir()
        (self.root / "tests").mkdir()
        (self.root / "app" / "client.py").write_text(
            "class Client:\n"
            "    def request(self, url):\n"
            "        return url\n",
            encoding="utf-8",
        )
        (self.root / "tests" / "test_client.py").write_text(
            "from app.client import Client\n"
            "def test_request():\n"
            "    assert Client().request('x') == 'x'\n",
            encoding="utf-8",
        )
        self.index = RepositoryIndex(self.root)
        self.context = ContextEngine().build(ContextRequest(
            "Add retry handling to Client.request and update its tests",
            self.index,
        ))
        self.capabilities = tuple(PlanOperation)

    def request(self, **kwargs) -> PlanningRequest:
        values = {
            "task": "Add bounded retry handling to Client.request",
            "context": self.context,
            "available_capabilities": self.capabilities,
            "workspace": PlanningWorkspace(self.root, self.index, "fixture"),
            "selected_model": "local-test-model",
        }
        values.update(kwargs)
        return PlanningRequest(**values)

    async def plan(self, raw: object, *, limits: PlannerLimits | None = None):
        provider = FakeProvider([json.dumps(raw)])
        result = await Planner(provider, limits).plan(self.request())
        return result, provider


class PlannerSchemaAndValidationTests(PlannerFixture):
    async def test_simple_read_plan_is_valid_without_modification_verification(self) -> None:
        raw = make_plan(
            operations=["read"], verification_criteria=[], verification_intent=[],
        )
        raw["completion_criteria"] = []
        raw["steps"][0]["description"] = "Inspect existing request behavior"
        raw["steps"][0]["expected_outcome"] = "Current behavior is understood"
        result, _ = await self.plan(raw)
        self.assertTrue(result.ok, result.errors)
        self.assertEqual(result.plan.verification_intent, [])

    async def test_valid_single_step_plan_uses_provider_without_tools(self) -> None:
        result, provider = await self.plan(make_plan())
        self.assertTrue(result.ok)
        self.assertIsInstance(result.plan, AgentPlan)
        self.assertEqual(result.plan.steps[0].operations, [PlanOperation.MODIFY])
        self.assertEqual(result.plan.verification_intent, [VerificationIntent.TARGETED_TESTS])
        self.assertEqual(result.plan.executable_order, ["step-1"])
        self.assertEqual(provider.requests[0][0], "local-test-model")
        self.assertEqual(provider.requests[0][2], [])
        self.assertEqual([message.role for message in provider.requests[0][1]], ["system", "user"])
        prompt = provider.requests[0][1][1].content
        self.assertIn("selection_is_exhaustive", prompt)
        self.assertIn("Syntactic callers are not a runtime call graph", prompt)
        self.assertEqual(result.attempts, 1)

    async def test_multi_step_dependencies_have_stable_executable_order(self) -> None:
        raw = make_plan()
        first = raw["steps"][0]
        second = dict(first)
        second.update({
            "id": "step-2",
            "description": "Add retry-focused tests",
            "purpose": "Prove retry behavior",
            "depends_on": ["step-1"],
            "paths": ["tests/test_client.py"],
            "symbols": ["tests.test_client.test_request"],
            "operations": ["modify", "test"],
        })
        raw["steps"].append(second)
        result, _ = await self.plan(raw)
        self.assertTrue(result.ok, result.errors)
        self.assertEqual(result.plan.executable_order, ["step-1", "step-2"])
        self.assertEqual(result.plan.steps[1].depends_on, ["step-1"])
        self.assertEqual(
            result.plan.steps[1].operations, [PlanOperation.MODIFY],
        )
        self.assertEqual(
            result.plan.verification_intent, [VerificationIntent.TARGETED_TESTS],
        )

    async def test_stable_topological_order_adjusts_model_order_to_respect_dependencies(self) -> None:
        raw = make_plan()
        dependent = dict(raw["steps"][0])
        dependent.update({"id": "step-2", "depends_on": ["step-1"]})
        prerequisite = dict(raw["steps"][0])
        prerequisite.update({
            "id": "step-1", "description": "Inspect request behavior",
            "purpose": "Establish current behavior", "operations": ["read"],
            "verification_criteria": [],
        })
        raw["steps"] = [dependent, prerequisite]
        result, _ = await self.plan(raw)
        self.assertTrue(result.ok, result.errors)
        self.assertEqual(result.plan.executable_order, ["step-1", "step-2"])
        self.assertTrue(any(
            issue.code == PlanningErrorCode.DEPENDENCY_ORDER_ADJUSTED
            for issue in result.warnings
        ))

    async def test_plan_rejects_missing_unknown_and_wrong_typed_fields(self) -> None:
        for mutate in (
            lambda value: value.pop("goal"),
            lambda value: value.update({"extra": True}),
            lambda value: value.update({"assumptions": "not an array"}),
            lambda value: value["steps"][0].update({"purpose": 3}),
        ):
            raw = make_plan()
            mutate(raw)
            result, _ = await self.plan(raw, limits=PlannerLimits(max_planning_attempts=1))
            self.assertFalse(result.ok)
            self.assertTrue(result.errors)

    async def test_plan_limits_cover_fields_arrays_steps_and_serialized_size(self) -> None:
        too_many = make_plan()
        too_many["steps"] *= 2
        result, _ = await self.plan(too_many, limits=PlannerLimits(max_plan_steps=1, max_planning_attempts=1))
        self.assertIn(PlanningErrorCode.TOO_MANY_STEPS, {error.code for error in result.errors})

        too_long = make_plan()
        too_long["steps"][0]["description"] = "x" * 50
        result, _ = await self.plan(
            too_long, limits=PlannerLimits(max_text_length=32, max_planning_attempts=1),
        )
        self.assertFalse(result.ok)

        result, _ = await self.plan(
            make_plan(), limits=PlannerLimits(max_total_plan_bytes=100, max_planning_attempts=1),
        )
        self.assertIn(PlanningErrorCode.PLAN_TOO_LARGE, {error.code for error in result.errors})

        long_completion = make_plan()
        long_completion["completion_criteria"] = ["x" * 1025]
        result, _ = await self.plan(
            long_completion, limits=PlannerLimits(max_planning_attempts=1),
        )
        self.assertFalse(result.ok)

        long_verification = make_plan(verification_criteria=["x" * 1025])
        result, _ = await self.plan(
            long_verification, limits=PlannerLimits(max_planning_attempts=1),
        )
        self.assertFalse(result.ok)

        paths = make_plan(paths=[f"app/file_{index}.py" for index in range(3)])
        result, _ = await self.plan(
            paths, limits=PlannerLimits(max_paths_per_step=2, max_planning_attempts=1),
        )
        self.assertFalse(result.ok)

    async def test_duplicate_and_malformed_step_ids_are_rejected(self) -> None:
        duplicate = make_plan()
        duplicate["steps"].append(dict(duplicate["steps"][0]))
        result, _ = await self.plan(duplicate, limits=PlannerLimits(max_planning_attempts=1))
        self.assertIn(PlanningErrorCode.DUPLICATE_STEP_ID, {error.code for error in result.errors})

        malformed = make_plan()
        malformed["steps"][0]["id"] = "../bad"
        result, _ = await self.plan(malformed, limits=PlannerLimits(max_planning_attempts=1))
        self.assertFalse(result.ok)

    async def test_unknown_self_and_cyclic_dependencies_are_rejected(self) -> None:
        cases = [
            ("step-unknown", PlanningErrorCode.UNKNOWN_DEPENDENCY),
            ("step-1", PlanningErrorCode.DEPENDENCY_CYCLE),
        ]
        for dependency, expected in cases:
            raw = make_plan()
            raw["steps"][0]["depends_on"] = [dependency]
            result, _ = await self.plan(raw, limits=PlannerLimits(max_planning_attempts=1))
            self.assertIn(expected, {error.code for error in result.errors})

        raw = make_plan()
        child = dict(raw["steps"][0])
        child.update({"id": "step-2", "depends_on": ["step-1"]})
        raw["steps"].append(child)
        raw["steps"][0]["depends_on"] = ["step-2"]
        result, _ = await self.plan(raw, limits=PlannerLimits(max_planning_attempts=1))
        self.assertIn(PlanningErrorCode.DEPENDENCY_CYCLE, {error.code for error in result.errors})

    async def test_operation_taxonomy_and_capabilities_are_enforced(self) -> None:
        raw = make_plan(operations=["run_shell"])
        result, _ = await self.plan(raw, limits=PlannerLimits(max_planning_attempts=1))
        self.assertIn(PlanningErrorCode.UNKNOWN_OPERATION, {error.code for error in result.errors})

        result, _ = await self.plan(
            make_plan(operations=["modify"]),
            limits=PlannerLimits(max_planning_attempts=1),
        )
        self.assertTrue(result.ok)
        restricted = self.request(available_capabilities=(PlanOperation.READ,))
        result = await Planner(FakeProvider([json.dumps(make_plan())]), PlannerLimits(
            max_planning_attempts=1,
        )).plan(restricted)
        self.assertIn(PlanningErrorCode.UNAVAILABLE_CAPABILITY, {error.code for error in result.errors})

    async def test_unknown_verification_category_is_rejected(self) -> None:
        result, _ = await self.plan(
            make_plan(verification_intent=["arbitrary_command"]),
            limits=PlannerLimits(max_planning_attempts=1),
        )
        self.assertIn(PlanningErrorCode.INVALID_SCHEMA, {error.code for error in result.errors})
        raw = make_plan()
        raw["steps"][0]["verification_intents"] = ["run_anything"]
        result, _ = await self.plan(raw, limits=PlannerLimits(max_planning_attempts=1))
        self.assertIn(PlanningErrorCode.INVALID_SCHEMA, {error.code for error in result.errors})

    async def test_path_validation_accepts_workspace_files_and_new_create_targets(self) -> None:
        result, _ = await self.plan(make_plan())
        self.assertTrue(result.ok, result.errors)

        (self.root / "tests" / "test_retries.py").unlink(missing_ok=True)
        raw = make_plan(
            paths=["tests/test_retries.py"], symbols=[],
            operations=["create"], verification_intent=["targeted_tests"],
        )
        result, _ = await self.plan(raw)
        self.assertTrue(result.ok, result.errors)

    async def test_test_and_verify_steps_are_normalized_to_typed_verification_intents(self) -> None:
        for operation in ("test", "verify"):
            with self.subTest(operation=operation):
                result, _ = await self.plan(
                    make_plan(operations=[operation]),
                )
                self.assertTrue(result.ok, result.errors)
                self.assertNotIn(
                    PlanOperation.TEST, result.plan.steps[0].operations,
                )
                self.assertNotIn(
                    PlanOperation.VERIFY, result.plan.steps[0].operations,
                )
                self.assertIn(
                    VerificationIntent.TARGETED_TESTS,
                    result.plan.verification_intent,
                )

    async def test_verify_step_without_typed_intent_is_rejected(self) -> None:
        raw = make_plan(operations=["verify"], verification_intent=[])
        result, _ = await self.plan(
            raw, limits=PlannerLimits(max_planning_attempts=1),
        )
        self.assertFalse(result.ok)
        self.assertIn(
            PlanningErrorCode.MISSING_VERIFICATION_INTENT,
            {issue.code for issue in result.errors},
        )

    async def test_required_outputs_are_explicit_plan_outcomes(self) -> None:
        raw = make_plan(required_outputs=["app/client.py"])
        result, _ = await self.plan(raw)
        self.assertTrue(result.ok, result.errors)
        self.assertEqual(result.plan.steps[0].required_outputs, ["app/client.py"])

    async def test_absolute_traversal_non_normalized_and_external_paths_are_rejected(self) -> None:
        for path in (
            str(self.root / "app" / "client.py"),
            "../outside.py",
            "app/../outside.py",
            "./app/client.py",
            "app//client.py",
        ):
            result, _ = await self.plan(
                make_plan(paths=[path]),
                limits=PlannerLimits(max_planning_attempts=1),
            )
            self.assertIn(PlanningErrorCode.INVALID_PATH, {error.code for error in result.errors})

    async def test_new_path_requires_create_operation(self) -> None:
        result, _ = await self.plan(
            make_plan(paths=["tests/new_test.py"]),
            limits=PlannerLimits(max_planning_attempts=1),
        )
        self.assertIn(PlanningErrorCode.INVALID_PATH, {error.code for error in result.errors})

    async def test_symlinked_paths_are_rejected(self) -> None:
        outside = self.root.parent / f"{self.root.name}-outside.py"
        outside.write_text("secret = True\n", encoding="utf-8")
        self.addCleanup(outside.unlink)
        (self.root / "app" / "linked.py").symlink_to(outside)
        result, _ = await self.plan(
            make_plan(paths=["app/linked.py"]),
            limits=PlannerLimits(max_planning_attempts=1),
        )
        self.assertIn(PlanningErrorCode.INVALID_PATH, {error.code for error in result.errors})

    async def test_create_rejects_symlink_parent_and_existing_target(self) -> None:
        outside_dir = self.root.parent / f"{self.root.name}-directory"
        outside_dir.mkdir()
        self.addCleanup(outside_dir.rmdir)
        (self.root / "external-link").symlink_to(outside_dir, target_is_directory=True)
        linked = make_plan(paths=["external-link/new.py"], operations=["create"])
        result, _ = await self.plan(linked, limits=PlannerLimits(max_planning_attempts=1))
        self.assertIn(PlanningErrorCode.INVALID_PATH, {error.code for error in result.errors})

    async def test_plan_paths_can_reference_files_outside_selected_context(self) -> None:
        raw = make_plan(paths=["tests/test_client.py"], symbols=[])
        result, _ = await self.plan(raw)
        self.assertTrue(result.ok, result.errors)

        exists = make_plan(paths=["app/client.py"], operations=["create"])
        result, _ = await self.plan(exists, limits=PlannerLimits(max_planning_attempts=1))
        self.assertIn(PlanningErrorCode.INVALID_PATH, {error.code for error in result.errors})

    async def test_context_known_unresolved_ambiguous_and_outside_symbols_are_warnings(self) -> None:
        known = make_plan()
        result, _ = await self.plan(known)
        self.assertTrue(result.ok, result.errors)

        unresolved = make_plan(symbols=["app.client.Client.nonexistent"])
        result, _ = await self.plan(unresolved)
        self.assertTrue(result.ok, result.errors)
        self.assertIn(PlanningErrorCode.UNRESOLVED_SYMBOL, {issue.code for issue in result.warnings})

        (self.root / "app" / "client.py").write_text(
            "class Client:\n"
            "    def request(self, url):\n"
            "        return url\n"
            "\n"
            "def future_helper():\n"
            "    return None\n",
            encoding="utf-8",
        )
        outside = make_plan(symbols=["app.client.future_helper"])
        result, _ = await self.plan(outside)
        self.assertTrue(result.ok, result.errors)
        self.assertIn(PlanningErrorCode.SYMBOL_OUTSIDE_CONTEXT, {issue.code for issue in result.warnings})

        (self.root / "app" / "other.py").write_text(
            "class Client:\n    def request(self):\n        return None\n",
            encoding="utf-8",
        )
        ambiguous = make_plan(symbols=["request"])
        result, _ = await self.plan(ambiguous)
        self.assertTrue(result.ok, result.errors)
        self.assertIn(PlanningErrorCode.AMBIGUOUS_SYMBOL, {issue.code for issue in result.warnings})

    async def test_context_limitations_and_truncation_are_preserved_as_warnings(self) -> None:
        limited = ContextEngine().build(ContextRequest(
            "Add retry handling to Client.request",
            self.index,
            budget=500,
        ))
        request = self.request(context=limited)
        result = await Planner(FakeProvider([json.dumps(make_plan())])).plan(request)
        self.assertTrue(result.ok, result.errors)
        self.assertTrue(result.plan.context_truncated)
        self.assertTrue(any(
            issue.code == PlanningErrorCode.CONTEXT_LIMITATION for issue in result.warnings
        ))
        self.assertTrue(result.plan.validation_warnings)

    async def test_context_warning_count_is_bounded_for_checkpoint_validation(self) -> None:
        context = replace(self.context, limitations=tuple(
            f"limitation {index}" for index in range(80)
        ))
        result = await Planner(FakeProvider([json.dumps(make_plan())])).plan(
            self.request(context=context),
        )
        self.assertTrue(result.ok, result.errors)
        self.assertEqual(len(result.warnings), 64)
        self.assertEqual(len(result.plan.validation_warnings), 64)
        self.assertIn("Additional planning warnings were omitted", result.plan.validation_warnings[-1])

    async def test_planner_prompt_clipping_is_reported_and_stored(self) -> None:
        provider = FakeProvider([json.dumps(make_plan())])
        result = await Planner(provider, PlannerLimits(
            max_context_items=1,
            max_prompt_context_characters=8,
        )).plan(self.request())
        self.assertTrue(result.ok, result.errors)
        self.assertTrue(result.context_truncated)
        self.assertTrue(result.plan.context_truncated)
        self.assertTrue(any(
            issue.code == PlanningErrorCode.CONTEXT_LIMITATION for issue in result.warnings
        ))
        context_json = json.loads(provider.requests[0][1][1].content)["context"]
        self.assertTrue(context_json["prompt_context_truncated"])

    async def test_modifying_plan_requires_target_completion_and_verification(self) -> None:
        raw = make_plan(paths=[], symbols=[])
        result, _ = await self.plan(raw, limits=PlannerLimits(max_planning_attempts=1))
        self.assertIn(PlanningErrorCode.SEMANTIC_INCONSISTENCY, {item.code for item in result.errors})

        raw = make_plan(verification_criteria=[], verification_intent=[])
        result, _ = await self.plan(raw, limits=PlannerLimits(max_planning_attempts=1))
        self.assertIn(PlanningErrorCode.MISSING_VERIFICATION_INTENT, {item.code for item in result.errors})

    async def test_executable_commands_are_not_accepted_as_verification_criteria(self) -> None:
        raw = make_plan(verification_criteria=["pytest -q tests/test_client.py"])
        result, _ = await self.plan(raw, limits=PlannerLimits(max_planning_attempts=1))
        self.assertIn(PlanningErrorCode.INVALID_SCHEMA, {item.code for item in result.errors})

    async def test_delete_is_warned_and_rendered_as_destructive(self) -> None:
        raw = make_plan(operations=["delete"], verification_intent=["targeted_tests"])
        result, _ = await self.plan(raw)
        self.assertTrue(result.ok, result.errors)
        self.assertIn(PlanningErrorCode.DESTRUCTIVE_OPERATION, {item.code for item in result.warnings})
        rendered = render_plan(result.plan)
        self.assertIn("[DESTRUCTIVE]", rendered)
        self.assertEqual(render_plan(result.plan, max_characters=12), rendered[:12])


class PlannerProviderAndRepairTests(PlannerFixture):
    async def test_invalid_output_is_repaired_once_with_structured_feedback(self) -> None:
        invalid = make_plan()
        invalid["steps"][0]["depends_on"] = ["missing-step"]
        provider = FakeProvider([json.dumps(invalid), json.dumps(make_plan())])
        result = await Planner(provider).plan(self.request())
        self.assertTrue(result.ok, result.errors)
        self.assertEqual(result.attempts, 2)
        repair = json.loads(provider.requests[1][1][1].content)
        self.assertEqual(repair["repair_feedback"][0]["code"], "UNKNOWN_DEPENDENCY")
        self.assertIn("previous_invalid_output", repair)

    async def test_repeated_invalid_outputs_fail_at_attempt_limit(self) -> None:
        invalid = make_plan()
        invalid["steps"][0]["depends_on"] = ["unknown"]
        provider = FakeProvider([json.dumps(invalid), json.dumps(invalid), json.dumps(make_plan())])
        result = await Planner(provider, PlannerLimits(max_planning_attempts=2)).plan(self.request())
        self.assertFalse(result.ok)
        self.assertEqual(result.attempts, 2)
        self.assertEqual(len(provider.requests), 2)
        self.assertIn(PlanningErrorCode.UNKNOWN_DEPENDENCY, {item.code for item in result.errors})

    async def test_malformed_json_and_duplicate_json_keys_are_rejected(self) -> None:
        for output in ("Here is a plan.", '{"goal":"x","goal":"y"}'):
            result = await Planner(FakeProvider([output]), PlannerLimits(
                max_planning_attempts=1,
            )).plan(self.request())
            self.assertFalse(result.ok)
            self.assertIn(PlanningErrorCode.INVALID_MODEL_OUTPUT, {item.code for item in result.errors})

    async def test_provider_error_returns_structured_failure(self) -> None:
        result = await Planner(FakeProvider(failure=ProviderError("offline"))).plan(self.request())
        self.assertFalse(result.ok)
        self.assertEqual(result.attempts, 1)
        self.assertEqual(result.errors[0].code, PlanningErrorCode.PROVIDER_ERROR)

    async def test_planner_tool_calls_are_rejected_and_never_executed(self) -> None:
        class ToolProvider(FakeProvider):
            async def chat(self, model, messages, tools):
                self.requests.append((model, messages, tools))
                yield ChatEvent(
                    tool_calls=[{"function": {"name": "terminal", "arguments": {"command": "touch bad"}}}],
                    done=True,
                )

        provider = ToolProvider()
        result = await Planner(provider, PlannerLimits(max_planning_attempts=1)).plan(self.request())
        self.assertFalse(result.ok)
        self.assertEqual(result.errors[0].code, PlanningErrorCode.PROVIDER_ERROR)
        self.assertFalse((self.root / "bad").exists())
        self.assertEqual(provider.requests[0][2], [])

    async def test_streaming_response_is_collected_without_thinking_or_tool_requests(self) -> None:
        output = json.dumps(make_plan())

        class StreamingProvider(FakeProvider):
            async def chat(self, model, messages, tools):
                self.requests.append((model, messages, tools))
                yield ChatEvent(content=output[:20])
                yield ChatEvent(thinking="private reasoning", content=output[20:], done=True)

        result = await Planner(StreamingProvider()).plan(self.request())
        self.assertTrue(result.ok, result.errors)
        self.assertNotIn("private reasoning", result.plan.goal)

    async def test_truncated_provider_stream_is_rejected(self) -> None:
        class TruncatedProvider(FakeProvider):
            async def chat(self, model, messages, tools):
                self.requests.append((model, messages, tools))
                yield ChatEvent(content='{"goal":"incomplete"}')

        result = await Planner(TruncatedProvider(), PlannerLimits(
            max_planning_attempts=1,
        )).plan(self.request())
        self.assertFalse(result.ok)
        self.assertEqual(result.errors[0].code, PlanningErrorCode.PROVIDER_ERROR)

    async def test_threading_cancellation_interrupts_stalled_provider_stream(self) -> None:
        cancelled = threading.Event()

        class SlowProvider(FakeProvider):
            async def chat(self, model, messages, tools):
                self.requests.append((model, messages, tools))
                yield ChatEvent(content="{")
                await asyncio.sleep(5)
                yield ChatEvent(content="}", done=True)

        timer = threading.Timer(0.05, cancelled.set)
        timer.start()
        self.addCleanup(timer.cancel)
        result = await Planner(SlowProvider()).plan(self.request(), cancelled)
        self.assertFalse(result.ok)
        self.assertEqual(result.errors[0].code, PlanningErrorCode.CANCELLED)
        self.assertIsNone(result.plan)

    async def test_oversized_planner_stream_is_bounded(self) -> None:
        provider = FakeProvider([json.dumps(make_plan())])
        result = await Planner(provider, PlannerLimits(
            max_response_characters=100, max_planning_attempts=1,
        )).plan(self.request())
        self.assertFalse(result.ok)
        self.assertEqual(result.errors[0].code, PlanningErrorCode.PROVIDER_ERROR)


class PlannerPersistenceAndParserTests(PlannerFixture):
    async def test_valid_plan_attaches_to_phase_one_checkpoint_and_roundtrips(self) -> None:
        result, _ = await self.plan(make_plan())
        task = AgentTask(result.plan.goal, selected_model=None)
        task.transition(AgentStatus.UNDERSTANDING)
        task.transition(AgentStatus.CONTEXT_GATHERING)
        task.transition(AgentStatus.PLANNING)
        attach_validated_plan(task, result)
        restored = AgentTask.from_dict(task.to_dict())
        checkpoint = AgentCheckpoint.from_dict(AgentCheckpoint(restored).to_dict())
        self.assertEqual(checkpoint.task.plan, result.plan)
        self.assertEqual(checkpoint.task.selected_model, "local-test-model")

    async def test_planned_checkpoint_roundtrips_through_history_schema_six(self) -> None:
        result, _ = await self.plan(make_plan())
        task = AgentTask(result.plan.goal)
        task.transition(AgentStatus.UNDERSTANDING)
        task.transition(AgentStatus.CONTEXT_GATHERING)
        task.transition(AgentStatus.PLANNING)
        attach_validated_plan(task, result)
        settings = replace(Settings(), history_dir=self.root / ".synai")
        history = History(self.root / "history", settings)
        session = Session(
            "local-test-model", settings.ollama_url, str(self.root),
        )
        session.set_environment(ConversationEnvironment.from_settings(settings, self.root))
        session.agent_checkpoint = AgentCheckpoint(task)
        history.save(session)
        restored = history.load(history.path_for(session.session_id))
        self.assertEqual(restored.agent_checkpoint.task.plan, result.plan)
        self.assertEqual(restored.schema_version, 6)

    async def test_invalid_plan_cannot_be_attached(self) -> None:
        result, _ = await self.plan(
            make_plan(paths=["../escape"]),
            limits=PlannerLimits(max_planning_attempts=1),
        )
        task = AgentTask("Add bounded retry handling to Client.request")
        task.transition(AgentStatus.UNDERSTANDING)
        task.transition(AgentStatus.CONTEXT_GATHERING)
        task.transition(AgentStatus.PLANNING)
        with self.assertRaisesRegex(ValueError, "Invalid planning results"):
            attach_validated_plan(task, result)
        self.assertIsNone(task.plan)

    async def test_legacy_phase_one_plan_data_remains_readable(self) -> None:
        legacy = {
            "plan_id": "stable-id",
            "goal": "Read a file",
            "created_at": "2026-01-01T00:00:00+00:00",
            "steps": [{
                "step_id": "step-1",
                "description": "Inspect source",
                "verification": None,
                "status": "pending",
            }],
        }
        restored = AgentPlan.from_dict(legacy)
        self.assertEqual(restored.goal, "Read a file")
        self.assertEqual(restored.steps[0].operations, [])
        self.assertEqual(restored.schema_version, 2)

    async def test_plan_renderer_does_not_mutate_structured_plan(self) -> None:
        result, _ = await self.plan(make_plan())
        before = result.plan.to_dict()
        render_plan(result.plan)
        self.assertEqual(result.plan.to_dict(), before)


if __name__ == "__main__":
    unittest.main()
