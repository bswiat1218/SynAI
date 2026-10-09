from __future__ import annotations

import json
import hashlib
import tempfile
import threading
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, AsyncIterator

from synai.coding_agent import (
    AgentCheckpoint,
    AgentExecution,
    AgentPlan,
    AgentStatus,
    AgentStep,
    AgentTask,
    CodingAgentRuntime,
    ExecutionStatus,
    PlanOperation,
    ReviewOutcome,
    ReviewLimits,
    ReviewSeverity,
    StepStatus,
    VerificationIntent,
    VerificationOutcome,
    ModelProfile,
    ModelRole,
    ModelRouter,
    RoleCandidates,
    RoutingConfig,
    RoutingMode,
    RoutingStrategy,
    TaskRouting,
    estimate_complexity,
)
from synai.coding_agent.routing import endpoint_fingerprint, session_fingerprint
from synai.coding_agent.context import (
    ContextConfidence,
    ContextItem,
    ContextKind,
    ContextPackage,
)
from synai.coding_agent.changes import TaskChangeTracker
from synai.coding_agent.reviewer import ReviewEngine
from synai.config import ConversationEnvironment, Settings
from synai.intelligence import RepositoryIndex
from synai.models import ChatEvent, ModelInfo, Session
from synai.tools import Tools


MODEL = "review-model"
GOAL = "Implement bounded request handling and keep its behavior correct."


def empty_review(summary: str = "No material issues found.") -> str:
    return json.dumps({"summary": summary, "findings": []})


class ReviewProvider:
    def __init__(self, responses: list[Any] | None = None) -> None:
        self.responses = list(responses or [empty_review()])
        self.calls: list[tuple[str, list[Any], list[dict[str, Any]]]] = []
        self.models = {MODEL: True, "reviewer-model": True}

    async def list_models(self) -> list[ModelInfo]:
        return [ModelInfo(name) for name in sorted(self.models)]

    async def capabilities(self, name: str) -> ModelInfo:
        return ModelInfo(name, tools=self.models[name])

    async def chat(
        self,
        model: str,
        messages: list[Any],
        tools: list[dict[str, Any]],
    ) -> AsyncIterator[ChatEvent]:
        self.calls.append((model, list(messages), list(tools)))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        if isinstance(response, tuple) and response[0] == "tool":
            yield ChatEvent(tool_calls=[{"function": {"name": "write_file"}}], done=True)
            return
        yield ChatEvent(content=str(response), done=True)


class VerificationBackend:
    def __init__(self, workspace: Path, settings: Settings) -> None:
        self.workspace = workspace.resolve()
        self.settings = settings
        self.calls: list[dict[str, Any]] = []

    def matches(self, session: Session) -> bool:
        return Path(session.workspace) == self.workspace

    async def execute(
        self,
        name: str,
        arguments: dict[str, Any],
        expected_sha256: str | None = None,
    ) -> dict[str, Any]:
        del expected_sha256
        self.calls.append({"name": name, **arguments})
        if name != "terminal":
            raise AssertionError(f"Review must not dispatch {name!r}")
        return {
            "ok": True,
            "stdout": "verification passed\n",
            "stderr": "",
            "exit_code": 0,
            "duration": 0.01,
            "timed_out": False,
            "truncated": False,
        }


class CodingAgentReviewerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / "project"
        (self.root / "app").mkdir(parents=True)
        (self.root / "tests").mkdir()
        (self.root / "app" / "client.py").write_text(
            "def request(value):\n    return value\n",
            encoding="utf-8",
        )
        self.test_path = self.root / "tests" / "test_client.py"
        self.test_path.write_text(
            "import unittest\n\n"
            "class RequestTests(unittest.TestCase):\n"
            "    def test_request(self):\n"
            "        self.assertTrue(request(1))\n",
            encoding="utf-8",
        )
        (self.root / "pyproject.toml").write_text(
            "[project]\nname = 'review-fixture'\nversion = '0.1.0'\n",
            encoding="utf-8",
        )
        self.settings = replace(
            Settings(execution_mode="host"),
            history_dir=self.base / ".synai",
            command_timeout=5,
            tool_budget=20,
        )
        self.backend = VerificationBackend(self.root, self.settings)
        self.session = Session(MODEL, self.settings.ollama_url, str(self.root))
        self.session.set_environment(
            ConversationEnvironment.from_settings(self.settings, self.root),
        )
        self.repository = RepositoryIndex(self.root)
        self.provider = ReviewProvider()
        self.runtime = CodingAgentRuntime(
            self.provider,
            Tools(self.backend, self._approve),
        )

    async def _approve(self, _name: str, _description: str) -> bool:
        del _name, _description
        return True

    def make_task(self, *, include_test_change: bool = False) -> AgentTask:
        paths = ["app/client.py"]
        if include_test_change:
            paths.append("tests/test_client.py")
        step = AgentStep(
            "step-1",
            "Implement bounded request behavior",
            purpose="Preserve the expected request result",
            paths=paths,
            operations=[PlanOperation.MODIFY],
            expected_outcome="The request implementation returns the expected value",
            verification_criteria=["The changed files compile"],
        )
        step.status = StepStatus.COMPLETED
        task = AgentTask(GOAL, selected_model=MODEL, plan=AgentPlan(
            GOAL,
            [step],
            completion_criteria=["Request behavior remains correct"],
            verification_intent=[VerificationIntent.SYNTAX_CHECK],
            executable_order=[step.step_id],
            context_hash="0" * 64,
            planner_provider=type(self.provider).__name__,
            planner_model=MODEL,
            planning_attempts=1,
        ))
        task.executions.append(AgentExecution(
            step_id=step.step_id,
            tool_name="patch_file",
            status=ExecutionStatus.SUCCEEDED,
            operation=PlanOperation.MODIFY,
            target_path="app/client.py",
            result_summary="Updated the request implementation.",
        ))
        if include_test_change:
            task.executions.append(AgentExecution(
                step_id=step.step_id,
                tool_name="patch_file",
                status=ExecutionStatus.SUCCEEDED,
                operation=PlanOperation.MODIFY,
                target_path="tests/test_client.py",
                result_summary="Updated request assertion.",
            ))
        for state in (
            AgentStatus.UNDERSTANDING,
            AgentStatus.CONTEXT_GATHERING,
            AgentStatus.PLANNING,
            AgentStatus.IMPLEMENTING,
            AgentStatus.VERIFYING,
        ):
            task.transition(state)
        return task

    async def verified_task(self, *, include_test_change: bool = False) -> AgentTask:
        task = self.make_task(include_test_change=include_test_change)
        result = await self.runtime.run_verification(
            task, self.session, self.repository,
        )
        self.assertEqual(result.outcome.value, "passed", result.error)
        self.assertEqual(task.status, AgentStatus.REVIEWING)
        self.assertTrue(task.verification_plan.source_fingerprints)
        return task

    @staticmethod
    def issue(
        *,
        category: str = "correctness",
        severity: str = "high",
        confidence: str = "high",
        path: str = "app/client.py",
        line: int = 2,
        evidence: str = "    return value",
        description: str = "The implementation does not satisfy the requested behavior.",
        blocking: bool = True,
    ) -> dict[str, Any]:
        return {
            "category": category,
            "severity": severity,
            "confidence": confidence,
            "path": path,
            "symbol": "request",
            "start_line": line,
            "end_line": line,
            "description": description,
            "evidence": evidence,
            "impact": "The requested behavior may be incorrect despite passing syntax checks.",
            "recommendation": "Implement and verify the stated behavior without widening plan scope.",
            "plan_step_id": "step-1",
            "execution_id": None,
            "suggested_blocking": blocking,
        }

    async def test_passing_review_completes_task_and_never_mutates_source(self) -> None:
        task = await self.verified_task()
        original = (self.root / "app" / "client.py").read_bytes()
        calls_before_review = len(self.backend.calls)
        checkpoints: list[dict[str, Any]] = []

        async def checkpoint(value: AgentCheckpoint) -> None:
            checkpoints.append(value.to_dict())

        result = await self.runtime.run_review(
            task,
            self.session,
            self.repository,
            checkpoint=checkpoint,
        )

        self.assertEqual(result.outcome, ReviewOutcome.PASSED)
        self.assertEqual(task.status, AgentStatus.COMPLETED)
        self.assertEqual(result.findings, ())
        self.assertEqual((self.root / "app" / "client.py").read_bytes(), original)
        self.assertEqual(len(self.backend.calls), calls_before_review)
        self.assertEqual(self.provider.calls[-1][2], [])
        self.assertTrue(checkpoints)
        restored = AgentCheckpoint.from_dict(checkpoints[-1])
        self.assertEqual(restored.task.status, AgentStatus.COMPLETED)
        self.assertEqual(restored.task.review_record.outcome, ReviewOutcome.PASSED)

    async def test_observer_failure_after_persisted_completion_does_not_revert_result(self) -> None:
        task = await self.verified_task()
        saved: list[AgentCheckpoint] = []

        async def checkpoint(value: AgentCheckpoint) -> None:
            saved.append(value)

        async def event_sink(event: Any) -> None:
            if event.kind == "task_completed":
                raise RuntimeError("observer unavailable")

        with self.assertLogs("synai.coding_agent.reviewer", level="WARNING"):
            result = await self.runtime.run_review(
                task,
                self.session,
                self.repository,
                checkpoint=checkpoint,
                event_sink=event_sink,
            )

        self.assertEqual(result.outcome, ReviewOutcome.PASSED)
        self.assertEqual(result.state, AgentStatus.COMPLETED)
        self.assertEqual(task.status, AgentStatus.COMPLETED)
        self.assertEqual(task.review_record.outcome, ReviewOutcome.PASSED)
        self.assertEqual(saved[-1].task.status, AgentStatus.COMPLETED)

    async def test_high_confidence_correctness_finding_requests_changes(self) -> None:
        task = await self.verified_task()
        response = json.dumps({
            "summary": "The implementation misses the requested result behavior.",
            "findings": [self.issue()],
        })
        self.provider.responses = [response]
        before = (self.root / "app" / "client.py").read_bytes()

        result = await self.runtime.run_review(task, self.session, self.repository)

        self.assertEqual(result.outcome, ReviewOutcome.CHANGES_REQUESTED, result.error)
        self.assertEqual(task.status, AgentStatus.REVIEWING)
        self.assertEqual(result.findings[0].severity, ReviewSeverity.HIGH)
        self.assertTrue(result.findings[0].blocking)
        self.assertEqual((self.root / "app" / "client.py").read_bytes(), before)
        self.assertEqual(len(self.backend.calls), 1)

    async def test_high_severity_finding_blocks_despite_false_model_suggestion(self) -> None:
        task = await self.verified_task()
        self.provider.responses = [json.dumps({
            "summary": "A material verified correctness defect remains.",
            "findings": [self.issue(
                severity="critical",
                blocking=False,
            )],
        })]

        result = await self.runtime.run_review(task, self.session, self.repository)

        self.assertEqual(result.outcome, ReviewOutcome.CHANGES_REQUESTED)
        self.assertTrue(result.findings[0].blocking)
        finding_data = result.findings[0].to_dict()
        self.assertIn("blocking", finding_data)
        self.assertNotIn("suggested_blocking", finding_data)

    async def test_suspicious_test_change_is_reported_but_not_edited(self) -> None:
        self.test_path.write_text(
            "import unittest\n\n"
            "class RequestTests(unittest.TestCase):\n"
            "    def test_request(self):\n"
            "        self.assertTrue(True)\n",
            encoding="utf-8",
        )
        task = await self.verified_task(include_test_change=True)
        response = json.dumps({
            "summary": "The changed test assertion no longer checks request behavior.",
            "findings": [self.issue(
                category="test_integrity",
                path="tests/test_client.py",
                line=5,
                evidence="        self.assertTrue(True)",
                description="The changed test now asserts a constant truth value.",
            )],
        })
        self.provider.responses = [response]
        before = self.test_path.read_bytes()

        result = await self.runtime.run_review(task, self.session, self.repository)

        self.assertEqual(result.outcome, ReviewOutcome.CHANGES_REQUESTED)
        self.assertEqual(result.findings[0].category.value, "test_integrity")
        self.assertEqual(self.test_path.read_bytes(), before)
        self.assertTrue(any(
            source["path"] == "tests/test_client.py"
            for source in json.loads(self.provider.calls[-1][1][1].content)["review_sources"]
        ))
        payload = json.loads(self.provider.calls[-1][1][1].content)
        self.assertIn("cannot claim a complete before/after diff", payload["diff_limitation"])

    async def test_reviewer_receives_verified_task_specific_assertion_diff(self) -> None:
        task = self.make_task(include_test_change=True)
        tracker = TaskChangeTracker(
            task, self.repository, self.settings, str(self.root),
        )
        replacement = (
            "import unittest\n\n"
            "class RequestTests(unittest.TestCase):\n"
            "    def test_request(self):\n"
            "        self.assertTrue(True)\n"
        )
        tracker.before_mutation(
            "test-integrity-execution",
            "tests/test_client.py",
            "modify",
            repair_attempt_id=None,
            tool_name="write_file",
            arguments={"path": "tests/test_client.py", "content": replacement},
        )
        self.test_path.write_text(replacement, encoding="utf-8")
        tracker.after_mutation("test-integrity-execution", {"ok": True})

        evidence, limitations = ReviewEngine(self.runtime)._task_change_evidence(
            SimpleNamespace(task=task, repository=self.repository),
            ("tests/test_client.py",),
        )

        self.assertEqual(limitations, ())
        task_diff = next(item for item in evidence if item["kind"] == "task_diff")
        self.assertTrue(task_diff["complete"])
        self.assertIn("-        self.assertTrue(request(1))", task_diff["diff"])
        self.assertIn("+        self.assertTrue(True)", task_diff["diff"])
        self.assertEqual(self.provider.calls, [])

    async def test_stale_verified_source_blocks_without_calling_model(self) -> None:
        task = await self.verified_task()
        path = self.root / "app" / "client.py"
        path.write_text("def request(value):\n    return None\n", encoding="utf-8")

        result = await self.runtime.run_review(task, self.session, self.repository)

        self.assertEqual(result.outcome, ReviewOutcome.BLOCKED, result.error)
        self.assertEqual(task.status, AgentStatus.REVIEWING)
        self.assertIn("stale", result.error)
        self.assertEqual(self.provider.calls, [])

    async def test_malformed_response_retries_once_and_empty_findings_pass(self) -> None:
        task = await self.verified_task()
        self.provider.responses = ["not-json", empty_review("No material concerns.")]

        result = await self.runtime.run_review(task, self.session, self.repository)

        self.assertIsNone(result.error, result.error)
        self.assertEqual(result.outcome, ReviewOutcome.PASSED)
        self.assertEqual(len(self.provider.calls), 2)
        self.assertTrue(all(call[2] == [] for call in self.provider.calls))

    async def test_unsupported_severity_retries_with_strict_schema(self) -> None:
        task = await self.verified_task()
        invalid = json.dumps({
            "summary": "Review",
            "findings": [self.issue(severity="urgent")],
        })
        self.provider.responses = [invalid, empty_review()]

        result = await self.runtime.run_review(task, self.session, self.repository)

        self.assertEqual(result.outcome, ReviewOutcome.PASSED)
        self.assertEqual(len(self.provider.calls), 2)

    async def test_invalid_path_retries_and_rejects_unreviewed_sources(self) -> None:
        task = await self.verified_task()
        invalid = json.dumps({
            "summary": "Review",
            "findings": [self.issue(path="../outside.py")],
        })
        self.provider.responses = [invalid, empty_review()]

        result = await self.runtime.run_review(task, self.session, self.repository)

        self.assertEqual(result.outcome, ReviewOutcome.PASSED)
        self.assertEqual(len(self.provider.calls), 2)

    async def test_nonexistent_reviewed_line_retries(self) -> None:
        task = await self.verified_task()
        invalid = json.dumps({
            "summary": "Review",
            "findings": [self.issue(line=999)],
        })
        self.provider.responses = [invalid, empty_review()]

        result = await self.runtime.run_review(task, self.session, self.repository)

        self.assertEqual(result.outcome, ReviewOutcome.PASSED)
        self.assertEqual(len(self.provider.calls), 2)

    async def test_missing_verification_is_blocked_without_model_call(self) -> None:
        task = self.make_task()
        task.transition(AgentStatus.REVIEWING)

        result = await self.runtime.run_review(task, self.session, self.repository)

        self.assertEqual(result.outcome, ReviewOutcome.BLOCKED)
        self.assertNotEqual(task.status, AgentStatus.COMPLETED)
        self.assertEqual(self.provider.calls, [])

    async def test_unexpected_provider_tool_call_is_rejected(self) -> None:
        task = await self.verified_task()
        self.provider.responses = [("tool", None)]

        result = await self.runtime.run_review(task, self.session, self.repository)

        self.assertEqual(result.outcome, ReviewOutcome.ERROR)
        self.assertNotEqual(task.status, AgentStatus.COMPLETED)
        self.assertEqual(self.provider.calls[-1][2], [])
        self.assertEqual(len(self.backend.calls), 1)

    async def test_pre_cancelled_review_preserves_verified_state(self) -> None:
        task = await self.verified_task()
        cancellation = threading.Event()
        cancellation.set()

        result = await self.runtime.run_review(
            task, self.session, self.repository, cancellation=cancellation,
        )

        self.assertEqual(result.outcome, ReviewOutcome.CANCELLED)
        self.assertEqual(task.status, AgentStatus.REVIEWING)
        self.assertEqual(task.verification_outcome.value, "passed")
        self.assertEqual(self.provider.calls, [])

    async def test_cancellation_during_completion_persistence_is_recorded(self) -> None:
        task = await self.verified_task()
        cancellation = threading.Event()
        checkpoints: list[AgentStatus] = []

        async def checkpoint(value: AgentCheckpoint) -> None:
            checkpoints.append(value.task.status)
            if value.task.status == AgentStatus.COMPLETED:
                cancellation.set()

        result = await self.runtime.run_review(
            task,
            self.session,
            self.repository,
            cancellation=cancellation,
            checkpoint=checkpoint,
        )

        self.assertEqual(result.outcome, ReviewOutcome.CANCELLED)
        self.assertEqual(task.status, AgentStatus.CANCELLED)
        self.assertEqual(task.verification_outcome.value, "passed")
        self.assertIn(AgentStatus.COMPLETED, checkpoints)
        self.assertEqual(task.review_record.outcome, ReviewOutcome.CANCELLED)

    async def test_failed_current_verification_outcome_is_blocked(self) -> None:
        task = await self.verified_task()
        task.verification_outcome = VerificationOutcome.CODE_FAILURE

        result = await self.runtime.run_review(task, self.session, self.repository)

        self.assertEqual(result.outcome, ReviewOutcome.BLOCKED)
        self.assertEqual(self.provider.calls, [])

    async def test_routed_review_uses_independent_model_with_no_tools(self) -> None:
        task = await self.verified_task()
        config = RoutingConfig(
            enabled=True,
            mode=RoutingMode.ROUTED,
            strategy=RoutingStrategy.CAPABILITY_FIRST,
            profiles=(
                ModelProfile(MODEL, roles=(ModelRole.PLANNING, ModelRole.IMPLEMENTATION)),
                ModelProfile("reviewer-model", roles=(ModelRole.REVIEW,)),
            ),
            roles=(
                RoleCandidates(ModelRole.PLANNING, (MODEL,)),
                RoleCandidates(ModelRole.IMPLEMENTATION, (MODEL,)),
                RoleCandidates(ModelRole.REVIEW, ("reviewer-model",)),
            ),
        )
        self.runtime.routing_config = config
        routing = TaskRouting(
            RoutingMode.ROUTED, config.fingerprint(), type(self.provider).__name__,
            endpoint_fingerprint(self.session.endpoint),
            session_fingerprint(self.session.session_id),
        )
        router = ModelRouter()
        for role, stage, model, require_tools in (
            (ModelRole.PLANNING, "planning", MODEL, False),
            (ModelRole.IMPLEMENTATION, "implementation", MODEL, True),
        ):
            routing.append(await router.select(
                self.provider, config, task_id=task.task_id, role=role,
                stage_id=stage, requested_model=MODEL, endpoint=self.session.endpoint,
                complexity=estimate_complexity(task.goal, plan=task.plan),
                require_tools=require_tools,
            ), task.task_id)
        task.routing = routing

        result = await self.runtime.run_review(task, self.session, self.repository)

        self.assertEqual(result.outcome, ReviewOutcome.PASSED, result.error)
        self.assertEqual(self.provider.calls[-1][0], "reviewer-model")
        self.assertEqual(self.provider.calls[-1][2], [])
        self.assertEqual(task.review_record.model, "reviewer-model")
        self.assertEqual(
            task.routing.assignment(
                ModelRole.REVIEW, f"review-{task.verification_plan.run_id}",
            ).selected_model,
            "reviewer-model",
        )

    async def test_uncertain_pending_execution_blocks_review(self) -> None:
        task = await self.verified_task()
        task.executions.append(AgentExecution(
            step_id="step-1",
            tool_name="patch_file",
            operation=PlanOperation.MODIFY,
            target_path="app/client.py",
        ))

        result = await self.runtime.run_review(task, self.session, self.repository)

        self.assertEqual(result.outcome, ReviewOutcome.BLOCKED)
        self.assertIn("uncertain pending", result.error)
        self.assertEqual(self.provider.calls, [])

    async def test_mismatched_verification_run_is_blocked(self) -> None:
        task = await self.verified_task()
        task.verification_plan.run_id = "different-verification-run"

        result = await self.runtime.run_review(task, self.session, self.repository)

        self.assertEqual(result.outcome, ReviewOutcome.BLOCKED)
        self.assertEqual(self.provider.calls, [])

    async def test_weakened_verification_requirements_are_blocked(self) -> None:
        task = await self.verified_task()
        required_check = next(
            check for check in task.verification_plan.checks if check.required
        )
        required_check.required = False

        result = await self.runtime.run_review(task, self.session, self.repository)

        self.assertEqual(result.outcome, ReviewOutcome.BLOCKED)
        self.assertIn("requirements", result.error)
        self.assertEqual(self.provider.calls, [])

    async def test_warning_does_not_block_but_is_persisted(self) -> None:
        task = await self.verified_task()
        self.provider.responses = [json.dumps({
            "summary": "There is a possible edge case worth checking.",
            "findings": [self.issue(
                severity="medium",
                confidence="low",
                blocking=True,
                description="A plausible but unconfirmed edge case may exist.",
            )],
        })]

        result = await self.runtime.run_review(task, self.session, self.repository)

        self.assertEqual(result.outcome, ReviewOutcome.PASSED_WITH_WARNINGS)
        self.assertEqual(task.status, AgentStatus.COMPLETED)
        self.assertFalse(result.findings[0].blocking)
        self.assertEqual(task.review_record.outcome, ReviewOutcome.PASSED_WITH_WARNINGS)

    async def test_valid_execution_evidence_is_path_bound_but_not_source_proof(self) -> None:
        task = await self.verified_task()
        execution_id = task.executions[0].execution_id
        self.provider.responses = [json.dumps({
            "summary": "The execution record is noted, but its summary alone does not prove a defect.",
            "findings": [self.issue(
                severity="critical",
                path="app/client.py",
                line=None,
                evidence="Updated the request implementation.",
                blocking=True,
            ) | {"execution_id": execution_id}],
        })]

        result = await self.runtime.run_review(task, self.session, self.repository)

        self.assertEqual(result.outcome, ReviewOutcome.PASSED_WITH_WARNINGS)
        self.assertFalse(result.findings[0].blocking)

    async def test_wrong_execution_path_is_rejected(self) -> None:
        task = await self.verified_task()
        execution_id = task.executions[0].execution_id
        mismatched = self.issue(
            path="tests/test_client.py",
            line=5,
            evidence="        self.assertTrue(request(1))",
        ) | {"execution_id": execution_id}
        self.provider.responses = [json.dumps({
            "summary": "Invalidly associated execution evidence.",
            "findings": [mismatched],
        }), empty_review()]

        result = await self.runtime.run_review(task, self.session, self.repository)

        self.assertEqual(result.outcome, ReviewOutcome.PASSED)
        self.assertEqual(len(self.provider.calls), 2)
        self.assertFalse(result.findings)

    async def test_invalid_execution_id_is_rejected(self) -> None:
        task = await self.verified_task()
        invalid = self.issue() | {"execution_id": "not-an-execution"}
        self.provider.responses = [json.dumps({
            "summary": "Unknown execution reference.",
            "findings": [invalid],
        }), empty_review()]

        result = await self.runtime.run_review(task, self.session, self.repository)

        self.assertEqual(result.outcome, ReviewOutcome.PASSED)
        self.assertEqual(len(self.provider.calls), 2)

    async def test_unsupported_critical_quote_cannot_block(self) -> None:
        task = await self.verified_task()
        unsupported = self.issue(
            severity="critical",
            evidence="This quote is absent from the verified source.",
            blocking=False,
        )
        self.provider.responses = [json.dumps({
            "summary": "Unsubstantiated critical allegation.",
            "findings": [unsupported],
        }), empty_review()]

        result = await self.runtime.run_review(task, self.session, self.repository)

        self.assertEqual(result.outcome, ReviewOutcome.PASSED)
        self.assertEqual(result.findings, ())
        self.assertEqual(len(self.provider.calls), 2)

    async def test_review_refreshes_stale_phase3_excerpt_before_prompting(self) -> None:
        historical_excerpt = "def request(value):\n    return value"
        item = ContextItem(
            ContextKind.FILE,
            "app/client.py",
            None,
            1,
            2,
            historical_excerpt,
            100,
            ("explicit_path",),
            "repository_index.read_source",
            "static_source_range",
            ContextConfidence.HIGH,
            len(historical_excerpt),
        )
        context = ContextPackage(
            GOAL,
            (item,),
            4096,
            0,
            item.estimated_cost,
            4096 - item.estimated_cost,
            False,
            (),
            (),
            (),
        )
        (self.root / "app" / "client.py").write_text(
            "def request(value):\n    return value + 2\n",
            encoding="utf-8",
        )
        task = self.make_task()
        result = await self.runtime.run_verification(
            task, self.session, self.repository, context=context,
        )
        self.assertEqual(result.outcome, VerificationOutcome.PASSED)

        review = await self.runtime.run_review(
            task, self.session, self.repository, context=context,
        )

        self.assertEqual(review.outcome, ReviewOutcome.PASSED, review.error)
        payload = json.loads(self.provider.calls[-1][1][1].content)
        current = next(
            source for source in payload["review_sources"]
            if source["path"] == "app/client.py"
        )
        self.assertEqual(current["freshness"], "current")
        self.assertIn("return value + 2", current["content"])
        historical = next(
            item for item in payload["phase3_context"]["items"]
            if item["path"] == "app/client.py"
        )
        self.assertEqual(historical["freshness"], "historical_only")
        self.assertEqual(historical["content"], "")
        self.assertIn("stale", " ".join(review.record.limitations).lower())

    async def test_legitimate_test_assertion_change_is_not_automatically_flagged(self) -> None:
        self.test_path.write_text(
            "import unittest\n\n"
            "class RequestTests(unittest.TestCase):\n"
            "    def test_request(self):\n"
            "        self.assertEqual(request(1), 1)\n",
            encoding="utf-8",
        )
        task = await self.verified_task(include_test_change=True)
        self.provider.responses = [empty_review()]

        result = await self.runtime.run_review(task, self.session, self.repository)

        self.assertEqual(result.outcome, ReviewOutcome.PASSED)
        self.assertEqual(result.findings, ())

    async def test_out_of_scope_mutation_is_reported_without_model_call(self) -> None:
        task = await self.verified_task()
        other_path = self.root / "app" / "unplanned.py"
        other_path.write_text("VALUE = 1\n", encoding="utf-8")
        task.executions.append(AgentExecution(
            step_id="step-1",
            tool_name="patch_file",
            status=ExecutionStatus.SUCCEEDED,
            operation=PlanOperation.MODIFY,
            target_path="app/unplanned.py",
            result_summary="Unexpected source modification.",
        ))
        task.verification_plan.source_fingerprints["app/unplanned.py"] = hashlib.sha256(
            other_path.read_bytes(),
        ).hexdigest()

        result = await self.runtime.run_review(task, self.session, self.repository)

        self.assertEqual(result.outcome, ReviewOutcome.CHANGES_REQUESTED)
        self.assertEqual(result.findings[0].category.value, "plan_alignment")
        self.assertEqual(self.provider.calls, [])

    async def test_unchanged_optional_planned_path_does_not_block_review(self) -> None:
        config = self.root / "app" / "config.py"
        config.write_text("RETRIES = 2\n", encoding="utf-8")
        task = self.make_task()
        task.plan.steps[0].paths.append("app/config.py")
        task.plan.validate()
        verified = await self.runtime.run_verification(
            task, self.session, self.repository,
        )
        self.assertEqual(verified.outcome, VerificationOutcome.PASSED)

        result = await self.runtime.run_review(task, self.session, self.repository)

        self.assertEqual(result.outcome, ReviewOutcome.PASSED)
        self.assertEqual(result.findings, ())

    async def test_multiple_planned_paths_and_mutations_are_reviewed(self) -> None:
        config = self.root / "app" / "config.py"
        config.write_text("RETRIES = 3\n", encoding="utf-8")
        task = self.make_task()
        task.plan.steps[0].paths.append("app/config.py")
        task.executions.append(AgentExecution(
            step_id="step-1",
            tool_name="patch_file",
            status=ExecutionStatus.SUCCEEDED,
            operation=PlanOperation.MODIFY,
            target_path="app/config.py",
            result_summary="Updated retry configuration.",
        ))
        task.plan.validate()
        verified = await self.runtime.run_verification(
            task, self.session, self.repository,
        )
        self.assertEqual(verified.outcome, VerificationOutcome.PASSED)

        result = await self.runtime.run_review(task, self.session, self.repository)

        self.assertEqual(result.outcome, ReviewOutcome.PASSED)
        self.assertEqual(result.findings, ())

    async def test_context_character_limit_blocks_before_model_request(self) -> None:
        task = await self.verified_task()

        result = await self.runtime.run_review(
            task,
            self.session,
            self.repository,
            limits=ReviewLimits(max_context_characters=1024),
        )

        self.assertEqual(result.outcome, ReviewOutcome.BLOCKED)
        self.assertEqual(self.provider.calls, [])

    async def test_provider_error_is_not_treated_as_pass(self) -> None:
        task = await self.verified_task()
        self.provider.responses = [RuntimeError("provider unavailable")]

        result = await self.runtime.run_review(task, self.session, self.repository)

        self.assertEqual(result.outcome, ReviewOutcome.ERROR)
        self.assertEqual(task.status, AgentStatus.REVIEWING)
        self.assertEqual(task.review_record.outcome, ReviewOutcome.ERROR)

    async def test_review_record_roundtrips_through_checkpoint(self) -> None:
        task = await self.verified_task()
        result = await self.runtime.run_review(task, self.session, self.repository)
        restored = AgentCheckpoint.from_dict(AgentCheckpoint(task).to_dict())

        self.assertEqual(result.outcome, ReviewOutcome.PASSED)
        self.assertEqual(restored.task.review_record.to_dict(), task.review_record.to_dict())


if __name__ == "__main__":
    unittest.main()
