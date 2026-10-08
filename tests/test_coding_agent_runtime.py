from __future__ import annotations

import asyncio
import hashlib
import json
import tempfile
import threading
import unittest
from dataclasses import replace
from pathlib import Path
from typing import Any

from synai.coding_agent import (
    AgentCheckpoint,
    AgentPlan,
    AgentStatus,
    AgentStep,
    AgentTask,
    AutonomyMode,
    CodingAgentRuntime,
    ContextEngine,
    ExecutionStatus,
    PlanApprovalDecision,
    PlanOperation,
    RuntimeErrorCode,
    RuntimeLimits,
    StepStatus,
    VerificationIntent,
)
from synai.coding_agent.checkpoints import CheckpointManager
from synai.config import ConversationEnvironment, Settings
from synai.history import History
from synai.intelligence import RepositoryIndex
from synai.models import ChatEvent, ModelInfo, Session
from synai.tools import Tools


TASK = "Add bounded retry handling to Client.request and update its tests."
MODEL = "local-runtime-model"


def plan_payload() -> dict[str, Any]:
    return {
        "goal": TASK,
        "assumptions": [],
        "uncertainties": [],
        "completion_criteria": ["Retry behavior is bounded and focused tests are updated"],
        "verification_intent": ["targeted_tests"],
        "steps": [
            {
                "id": "step-1",
                "description": "Implement bounded retries in Client.request",
                "purpose": "Handle transient request failures without changing the public interface",
                "depends_on": [],
                "paths": ["app/client.py"],
                "symbols": ["app.client.Client.request"],
                "operations": ["modify"],
                "expected_outcome": "Transient failures are retried a bounded number of times",
                "verification_criteria": ["The focused retry behavior can be verified"],
                "verification_intents": [],
            },
            {
                "id": "step-2",
                "description": "Update request tests for retry behavior",
                "purpose": "Cover bounded retries",
                "depends_on": ["step-1"],
                "paths": ["tests/test_client.py"],
                "symbols": ["tests.test_client.test_request"],
                "operations": ["modify"],
                "expected_outcome": "Focused request tests cover retry behavior",
                "verification_criteria": ["The updated tests can be verified"],
                "verification_intents": [],
            },
        ],
    }


def typed_plan(
    steps: list[AgentStep],
    *,
    verification: list[VerificationIntent] | None = None,
) -> AgentPlan:
    return AgentPlan(
        TASK,
        steps,
        completion_criteria=["The planned outcome is present"],
        verification_intent=(
            [VerificationIntent.TARGETED_TESTS] if verification is None else verification
        ),
        executable_order=[step.step_id for step in steps],
        context_hash="0" * 64,
        planner_provider="FakeProvider",
        planner_model=MODEL,
        planning_attempts=1,
    )


class FakeProvider:
    def __init__(
        self,
        *,
        plan: dict[str, Any] | None = None,
        execution: list[tuple[list[dict[str, Any]], str]] | None = None,
    ) -> None:
        self.plan = plan or plan_payload()
        self.execution = list(execution or [])
        self.requests: list[tuple[list[Any], list[dict[str, Any]]]] = []

    async def list_models(self) -> list[ModelInfo]:
        return [ModelInfo(MODEL, tools=True)]

    async def capabilities(self, name: str) -> ModelInfo:
        return ModelInfo(name, tools=True)

    async def chat(self, _model: str, messages: list[Any], tools: list[dict[str, Any]]):
        del _model
        self.requests.append((list(messages), list(tools)))
        if not tools:
            yield ChatEvent(content=json.dumps(self.plan), done=True)
            return
        if not self.execution:
            raise AssertionError("Unexpected execution-provider request")
        calls, content = self.execution.pop(0)
        yield ChatEvent(content=content, tool_calls=calls, done=True)


class FakeBackend:
    def __init__(self, root: Path, settings: Settings) -> None:
        self.root = root.resolve()
        self.workspace = self.root
        self.settings = settings
        self.calls: list[tuple[str, dict[str, Any], str | None]] = []
        self.block_mutation = False
        self.release_mutation = asyncio.Event()

    def matches(self, session: Session) -> bool:
        return Path(session.workspace) == self.workspace

    async def execute(
        self,
        name: str,
        arguments: dict[str, Any],
        expected_sha256: str | None = None,
    ) -> dict[str, Any]:
        self.calls.append((name, dict(arguments), expected_sha256))
        path = self.root / arguments.get("path", ".")
        if name == "preview":
            content = path.read_text(encoding="utf-8") if path.exists() else None
            digest = hashlib.sha256(content.encode()).hexdigest() if content is not None else None
            return {"ok": True, "content": content, "sha256": digest}
        if name == "read_file":
            return {"ok": True, "content": path.read_text(encoding="utf-8")}
        if name == "list_files":
            return {"ok": True, "entries": sorted(item.name for item in path.iterdir())}
        if name in {"write_file", "patch_file", "delete_file"}:
            if self.block_mutation:
                await self.release_mutation.wait()
            content = path.read_text(encoding="utf-8") if path.exists() else None
            digest = hashlib.sha256(content.encode()).hexdigest() if content is not None else None
            if digest != expected_sha256:
                return {"ok": False, "error": "File changed"}
            if name == "delete_file":
                path.unlink()
            elif name == "write_file":
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(arguments["content"], encoding="utf-8")
            else:
                old = arguments["old"]
                if content is None or content.count(old) != 1:
                    return {"ok": False, "error": "Patch must match exactly once"}
                path.write_text(content.replace(old, arguments["new"], 1), encoding="utf-8")
            return {"ok": True, "path": arguments["path"]}
        return {"ok": False, "error": "unsupported"}


class RuntimeFixture(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name)
        self.root = base / "project"
        self.root.mkdir()
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
        self.settings = replace(
            Settings(execution_mode="host"),
            history_dir=base / ".synai",
            tool_budget=20,
        )
        self.backend = FakeBackend(self.root, self.settings)
        self.session = Session(MODEL, self.settings.ollama_url, str(self.root))
        self.session.set_environment(ConversationEnvironment.from_settings(self.settings, self.root))
        self.repository = RepositoryIndex(self.root)

    def make_runtime(
        self,
        provider: FakeProvider,
        *,
        approve: Any = None,
        limits: RuntimeLimits | None = None,
        history: History | None = None,
    ) -> CodingAgentRuntime:
        self.approvals: list[str] = []

        async def approval(name: str, _description: str) -> bool:
            del _description
            self.approvals.append(name)
            return True if approve is None else bool(await approve(name))

        return CodingAgentRuntime(
            provider,
            Tools(self.backend, approval),
            context_engine=ContextEngine(),
            limits=limits,
            history=history,
        )

    def patch_call(self, path: str, old: str, new: str) -> list[dict[str, Any]]:
        return [{"function": {
            "name": "patch_file",
            "arguments": {"path": path, "old": old, "new": new},
        }}]

    async def test_end_to_end_implementation_stops_ready_for_verification(self) -> None:
        provider = FakeProvider(execution=[
            (
                self.patch_call(
                    "app/client.py",
                    "        return url",
                    "        for attempt in range(3):\n"
                    "            try:\n"
                    "                return url\n"
                    "            except TimeoutError:\n"
                    "                if attempt == 2:\n"
                    "                    raise",
                ),
                "Implementing bounded retries.",
            ),
            ([], "Source implementation updated."),
            (
                self.patch_call(
                    "tests/test_client.py",
                    "    assert Client().request('x') == 'x'",
                    "    assert Client().request('x') == 'x'  # retry case",
                ),
                "Updating the declared test file.",
            ),
            ([], "Test source updated."),
        ])
        runtime = self.make_runtime(provider)
        task = AgentTask(TASK)
        original_client = (self.root / "app" / "client.py").read_text(encoding="utf-8")
        saved: list[dict[str, Any]] = []
        events = []

        async def save(checkpoint: AgentCheckpoint) -> None:
            saved.append(checkpoint.to_dict())

        async def emit(event: Any) -> None:
            events.append(event)

        explicit_checkpoint = await runtime.tools.call(
            "git_checkpoint",
            {
                "task_id": task.task_id,
                "paths": ["app/client.py"],
                "require_complete": True,
            },
            session=self.session,
        )
        self.assertTrue(explicit_checkpoint["success"], explicit_checkpoint)

        result = await runtime.run_task(
            task, TASK, self.session, self.repository, model=MODEL,
            autonomy=AutonomyMode.AGENT, checkpoint=save, event_sink=emit,
        )

        self.assertTrue(result.ok, result.error)
        self.assertTrue(result.ready_for_verification)
        self.assertEqual(result.state, AgentStatus.VERIFYING)
        self.assertEqual(result.completed_steps, ("step-1", "step-2"))
        self.assertEqual([step.step_id for step in task.plan.steps if step.status == StepStatus.COMPLETED], [
            "step-1", "step-2",
        ])
        self.assertEqual(self.approvals, ["git_checkpoint", "patch_file", "patch_file"])
        self.assertEqual(len([item for item in task.executions if item.status == ExecutionStatus.SUCCEEDED]), 2)
        self.assertTrue(all(item.approval_state.value == "approved" for item in task.executions))
        self.assertEqual(
            {item.path for item in task.change_baselines},
            {"app/client.py", "tests/test_client.py"},
        )
        self.assertEqual(len(task.change_evidence), 2)
        self.assertTrue(all(item.outcome == "succeeded" for item in task.change_evidence))
        self.assertTrue(all(item.repair_attempt_id is None for item in task.change_evidence))
        checkpoint_manager = CheckpointManager(self.settings)
        checkpoint_record = checkpoint_manager.load(explicit_checkpoint["checkpoint_id"])
        self.assertEqual(
            checkpoint_record.files[0].expected_sha256,
            hashlib.sha256((self.root / "app" / "client.py").read_bytes()).hexdigest(),
        )
        self.assertIn("range(3)", (self.root / "app" / "client.py").read_text())
        self.assertIn("retry case", (self.root / "tests" / "test_client.py").read_text())
        self.assertEqual([name for name, _, _ in self.backend.calls].count("terminal"), 0)
        self.assertTrue(saved)
        self.assertEqual(events[-1].kind, "ready_for_verification")
        self.assertTrue(any(event.kind == "waiting_for_tool_approval" for event in events))
        self.assertTrue(task.plan.verification_intent == [VerificationIntent.TARGETED_TESTS])

        restored = await runtime.tools.call(
            "restore_checkpoint",
            {
                "checkpoint_id": explicit_checkpoint["checkpoint_id"],
                "paths": ["app/client.py"],
            },
            session=self.session,
        )
        self.assertTrue(restored["success"], restored)
        self.assertEqual((self.root / "app" / "client.py").read_text(), original_client)
        self.assertIn("retry case", (self.root / "tests" / "test_client.py").read_text())
        self.assertEqual(
            self.approvals,
            ["git_checkpoint", "patch_file", "patch_file", "restore_checkpoint", "write_file"],
        )

    async def test_observer_failure_does_not_fail_implementation_step(self) -> None:
        provider = FakeProvider(execution=[
            (
                self.patch_call("app/client.py", "        return url", "        return url + '!'"),
                "Patch the planned client.",
            ),
            ([], "The implementation is complete."),
        ])
        runtime = self.make_runtime(provider)
        task = AgentTask(TASK)

        async def event_sink(event: Any) -> None:
            if event.kind == "step_started":
                raise RuntimeError("observer unavailable")

        with self.assertLogs("synai.coding_agent.runtime", level="WARNING"):
            result = await runtime.run_task(
                task,
                TASK,
                self.session,
                self.repository,
                model=MODEL,
                plan=typed_plan([
                    AgentStep(
                        "step-1",
                        "Modify client",
                        paths=["app/client.py"],
                        operations=[PlanOperation.MODIFY],
                    ),
                ]),
                event_sink=event_sink,
            )

        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.state, AgentStatus.VERIFYING)
        self.assertEqual(task.plan.steps[0].status, StepStatus.COMPLETED)

    async def test_supervised_plan_denial_executes_no_tool_or_step(self) -> None:
        provider = FakeProvider()
        runtime = self.make_runtime(provider)
        task = AgentTask(TASK)
        approvals: list[str] = []

        async def deny(_task: AgentTask, rendered: str) -> PlanApprovalDecision:
            del _task
            approvals.append(rendered)
            return PlanApprovalDecision.DENIED

        result = await runtime.run_task(
            task, TASK, self.session, self.repository, model=MODEL,
            autonomy=AutonomyMode.SUPERVISED, plan_approval=deny,
        )

        self.assertFalse(result.ok)
        self.assertEqual(result.error.code, RuntimeErrorCode.PLAN_APPROVAL_DENIED)
        self.assertEqual(task.status, AgentStatus.FAILED)
        self.assertEqual(approvals, [result.rendered_plan])
        self.assertEqual(result.executions, ())
        self.assertEqual(self.backend.calls, [])
        self.assertEqual(self.approvals, [])
        self.assertEqual(len(provider.requests), 1)

    async def test_supervised_plan_without_callback_remains_pending(self) -> None:
        runtime = self.make_runtime(FakeProvider())
        task = AgentTask(TASK)
        result = await runtime.run_task(
            task, TASK, self.session, self.repository, model=MODEL,
            autonomy=AutonomyMode.SUPERVISED,
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.error.code, RuntimeErrorCode.PLAN_APPROVAL_PENDING)
        self.assertEqual(task.status, AgentStatus.WAITING_FOR_APPROVAL)
        self.assertEqual(result.executions, ())
        self.assertEqual(self.backend.calls, [])

    async def test_cancellation_while_waiting_for_plan_approval_runs_no_step(self) -> None:
        provider = FakeProvider()
        runtime = self.make_runtime(provider)
        task = AgentTask(TASK)
        cancellation = threading.Event()

        async def cancel_plan(_task: AgentTask, rendered: str) -> PlanApprovalDecision:
            del _task, rendered
            cancellation.set()
            return PlanApprovalDecision.CANCELLED

        result = await runtime.run_task(
            task, TASK, self.session, self.repository, model=MODEL,
            autonomy=AutonomyMode.SUPERVISED, plan_approval=cancel_plan,
            cancellation=cancellation,
        )
        self.assertEqual(result.state, AgentStatus.CANCELLED)
        self.assertEqual(result.error.code, RuntimeErrorCode.CANCELLED)
        self.assertEqual(result.executions, ())
        self.assertEqual(self.backend.calls, [])

    async def test_pending_plan_approval_can_resume_without_replanning(self) -> None:
        provider = FakeProvider(
            plan={
                "goal": TASK,
                "assumptions": [],
                "uncertainties": [],
                "completion_criteria": [],
                "verification_intent": [],
                "steps": [{
                    "id": "step-1",
                    "description": "Inspect the client",
                    "purpose": "Understand its current request behavior",
                    "depends_on": [],
                    "paths": ["app/client.py"],
                    "symbols": ["app.client.Client.request"],
                    "operations": ["read"],
                    "expected_outcome": "Current request behavior is understood",
                    "verification_criteria": [],
                    "verification_intents": [],
                }],
            },
            execution=[
                ([{"function": {
                    "name": "read_file", "arguments": {"path": "app/client.py"},
                }}], "Read the client source."),
                ([], "Current request behavior is understood."),
            ],
        )
        runtime = self.make_runtime(provider)

        async def pending(_task: AgentTask, _rendered: str) -> PlanApprovalDecision:
            del _task, _rendered
            return PlanApprovalDecision.PENDING

        task = AgentTask(TASK)
        waiting = await runtime.run_task(
            task, TASK, self.session, self.repository, model=MODEL,
            autonomy=AutonomyMode.SUPERVISED, plan_approval=pending,
        )
        self.assertEqual(waiting.state, AgentStatus.WAITING_FOR_APPROVAL)
        self.assertEqual(len(provider.requests), 1)

        from synai.coding_agent import ContextRequest

        context = ContextEngine().build(ContextRequest(TASK, self.repository))
        resumed = await runtime.resume_plan_approval(
            task, PlanApprovalDecision.APPROVED, context, self.session,
            self.repository, model=MODEL,
        )
        self.assertTrue(resumed.ok, resumed.error)
        self.assertEqual(resumed.state, AgentStatus.COMPLETED)
        self.assertEqual(len(provider.requests), 3)
        self.assertEqual(self.backend.calls[0][0], "read_file")

    async def test_undeclared_mutation_is_rejected_before_dispatch_and_approval(self) -> None:
        config = self.root / "app" / "config.py"
        config.write_text("enabled = False\n", encoding="utf-8")
        provider = FakeProvider(execution=[
            ([{"function": {
                "name": "write_file",
                "arguments": {"path": "app/config.py", "content": "enabled = True\n"},
            }}], "Trying an adjacent workspace file."),
        ])
        runtime = self.make_runtime(provider)
        plan = typed_plan([
            AgentStep(
                "step-1", "Modify the client", purpose="Change retry behavior",
                paths=["app/client.py"], operations=[PlanOperation.MODIFY],
                expected_outcome="Retry behavior changes",
                verification_criteria=["Focused retry behavior can be checked"],
            ),
        ])
        result = await runtime.run_task(
            AgentTask(TASK), TASK, self.session, self.repository,
            model=MODEL, plan=plan,
        )

        self.assertFalse(result.ok)
        self.assertEqual(result.error.code, RuntimeErrorCode.UNDECLARED_MUTATION_TARGET)
        self.assertEqual(config.read_text(), "enabled = False\n")
        self.assertEqual(self.approvals, [])
        self.assertEqual([name for name, _, _ in self.backend.calls], [])

    async def test_read_only_step_cannot_mutate(self) -> None:
        provider = FakeProvider(execution=[
            ([{"function": {
                "name": "write_file",
                "arguments": {"path": "app/client.py", "content": "unsafe"},
            }}], "Attempt write from read step."),
        ])
        runtime = self.make_runtime(provider)
        plan = typed_plan([
            AgentStep(
                "step-1", "Inspect the client", paths=["app/client.py"],
                operations=[PlanOperation.READ],
            ),
        ], verification=[])
        result = await runtime.run_task(
            AgentTask(TASK), TASK, self.session, self.repository,
            model=MODEL, plan=plan,
        )
        self.assertEqual(result.error.code, RuntimeErrorCode.TOOL_NOT_ALLOWED_FOR_STEP)
        self.assertEqual(self.backend.calls, [])
        self.assertEqual(self.approvals, [])

    async def test_create_then_modify_same_declared_path_is_preflighted_in_order(self) -> None:
        provider = FakeProvider(execution=[
            ([{"function": {
                "name": "write_file",
                "arguments": {"path": "app/new.py", "content": "value = 1\n"},
            }}], "Create the declared source file."),
            ([], "The file was created."),
            ([{"function": {
                "name": "patch_file",
                "arguments": {"path": "app/new.py", "old": "value = 1", "new": "value = 2"},
            }}], "Modify the newly created file."),
            ([], "The declared modification is complete."),
        ])
        runtime = self.make_runtime(provider)
        plan = typed_plan([
            AgentStep(
                "step-1", "Create the module", paths=["app/new.py"],
                operations=[PlanOperation.CREATE],
                expected_outcome="The new module exists",
            ),
            AgentStep(
                "step-2", "Update the module", depends_on=["step-1"],
                paths=["app/new.py"], operations=[PlanOperation.MODIFY],
                expected_outcome="The module contains the updated value",
            ),
        ])

        result = await runtime.run_task(
            AgentTask(TASK), TASK, self.session, self.repository,
            model=MODEL, plan=plan,
        )

        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.completed_steps, ("step-1", "step-2"))
        self.assertEqual((self.root / "app" / "new.py").read_text(), "value = 2\n")
        self.assertEqual(
            [name for name, _, _ in self.backend.calls],
            ["preview", "write_file", "preview", "patch_file"],
        )

    async def test_plan_created_for_different_model_is_rejected(self) -> None:
        runtime = self.make_runtime(FakeProvider())
        steps = [
            AgentStep(
                "step-1", "Inspect the client", paths=["app/client.py"],
                operations=[PlanOperation.READ],
            ),
        ]
        for plan in (
            replace(typed_plan(steps, verification=[]), planner_model="different-model"),
            replace(typed_plan(steps, verification=[]), planner_provider="OtherProvider"),
        ):
            with self.subTest(provider=plan.planner_provider, model=plan.planner_model):
                result = await runtime.run_task(
                    AgentTask(TASK), TASK, self.session, self.repository,
                    model=MODEL, plan=plan,
                )
                self.assertEqual(result.error.code, RuntimeErrorCode.PLAN_INVALID_AT_EXECUTION)
                self.assertEqual(self.backend.calls, [])

    async def test_supervised_approval_is_separate_from_tool_approval(self) -> None:
        provider = FakeProvider(execution=[
            (
                self.patch_call(
                    "app/client.py", "        return url", "        return url + '!'",
                ),
                "Patch the declared file.",
            ),
            ([], "Updated."),
            (
                self.patch_call(
                    "tests/test_client.py",
                    "    assert Client().request('x') == 'x'",
                    "    assert Client().request('x') == 'x'  # updated",
                ),
                "Patch the declared tests.",
            ),
            ([], "Updated tests."),
        ])
        runtime = self.make_runtime(provider)
        task = AgentTask(TASK)

        async def allow_plan(_task: AgentTask, _rendered: str) -> PlanApprovalDecision:
            del _task, _rendered
            return PlanApprovalDecision.APPROVED

        result = await runtime.run_task(
            task, TASK, self.session, self.repository, model=MODEL,
            autonomy=AutonomyMode.SUPERVISED, plan_approval=allow_plan,
        )

        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.state, AgentStatus.VERIFYING)
        self.assertEqual(self.approvals, ["patch_file", "patch_file"])

    async def test_tool_approval_denial_is_not_overridden_by_agent_mode(self) -> None:
        provider = FakeProvider(execution=[
            (
                self.patch_call("app/client.py", "        return url", "        return url + '!'"),
                "Patch declared source.",
            ),
        ])

        async def deny(_name: str) -> bool:
            del _name
            return False

        runtime = self.make_runtime(provider, approve=deny)
        result = await runtime.run_task(
            AgentTask(TASK), TASK, self.session, self.repository, model=MODEL,
            plan=typed_plan([
                AgentStep(
                    "step-1", "Modify client", paths=["app/client.py"],
                    operations=[PlanOperation.MODIFY],
                ),
            ]),
        )
        self.assertEqual(result.error.code, RuntimeErrorCode.APPROVAL_DENIED)
        self.assertEqual(result.executions[0].status, ExecutionStatus.DENIED)
        self.assertEqual(result.executions[0].approval_state.value, "denied")
        self.assertEqual([name for name, _, _ in self.backend.calls], ["preview"])
        self.assertEqual((self.root / "app" / "client.py").read_text().splitlines()[-1], "        return url")

    async def test_cancellation_during_tool_approval_vetoes_backend_dispatch(self) -> None:
        provider = FakeProvider(execution=[
            (
                self.patch_call("app/client.py", "        return url", "        return url + '!'"),
                "Patch the declared file.",
            ),
        ])
        cancellation = threading.Event()

        async def cancel_approval(_name: str) -> bool:
            del _name
            cancellation.set()
            return True

        runtime = self.make_runtime(provider, approve=cancel_approval)
        result = await runtime.run_task(
            AgentTask(TASK), TASK, self.session, self.repository, model=MODEL,
            plan=typed_plan([
                AgentStep(
                    "step-1", "Modify client", paths=["app/client.py"],
                    operations=[PlanOperation.MODIFY],
                ),
            ]),
            cancellation=cancellation,
        )
        self.assertEqual(result.error.code, RuntimeErrorCode.CANCELLED)
        self.assertEqual(result.executions[0].status, ExecutionStatus.CANCELLED)
        self.assertEqual([name for name, _, _ in self.backend.calls], ["preview"])

    async def test_backend_change_after_plan_approval_is_rejected(self) -> None:
        runtime = self.make_runtime(FakeProvider())
        task = AgentTask(TASK)

        async def change_workspace(_task: AgentTask, _rendered: str) -> PlanApprovalDecision:
            del _task, _rendered
            self.session.workspace = str(self.root / "other")
            return PlanApprovalDecision.APPROVED

        result = await runtime.run_task(
            task, TASK, self.session, self.repository, model=MODEL,
            autonomy=AutonomyMode.SUPERVISED, plan_approval=change_workspace,
        )
        self.assertEqual(result.error.code, RuntimeErrorCode.BACKEND_MISMATCH)
        self.assertEqual(result.executions, ())
        self.assertEqual(self.backend.calls, [])

    async def test_symlinked_plan_target_is_revalidated_before_execution(self) -> None:
        outside = Path(self.temp.name) / "outside.py"
        outside.write_text("secret = True\n", encoding="utf-8")
        (self.root / "app" / "link.py").symlink_to(outside)
        provider = FakeProvider()
        runtime = self.make_runtime(provider)
        result = await runtime.run_task(
            AgentTask(TASK), TASK, self.session, self.repository,
            model=MODEL,
            plan=typed_plan([
                AgentStep(
                    "step-1", "Inspect linked source", paths=["app/link.py"],
                    operations=[PlanOperation.READ],
                ),
            ], verification=[]),
        )
        self.assertEqual(result.error.code, RuntimeErrorCode.WORKSPACE_CHANGED)
        self.assertEqual(self.backend.calls, [])

    async def test_tool_failure_can_be_corrected_within_bounded_step(self) -> None:
        provider = FakeProvider(execution=[
            (
                self.patch_call("app/client.py", "missing text", "replacement"),
                "Try the stale patch.",
            ),
            (
                self.patch_call("app/client.py", "        return url", "        return url + '!'"),
                "Retry with the observed source.",
            ),
            ([], "The source update is complete."),
        ])
        runtime = self.make_runtime(provider, limits=RuntimeLimits(max_rounds_per_step=4))
        result = await runtime.run_task(
            AgentTask(TASK), TASK, self.session, self.repository,
            model=MODEL,
            plan=typed_plan([
                AgentStep(
                    "step-1", "Modify client", paths=["app/client.py"],
                    operations=[PlanOperation.MODIFY],
                ),
            ]),
        )
        self.assertTrue(result.ok, result.error)
        self.assertEqual(
            [item.status for item in result.executions],
            [ExecutionStatus.FAILED, ExecutionStatus.SUCCEEDED],
        )
        self.assertEqual(self.approvals, ["patch_file"])

    async def test_test_and_verify_steps_are_rejected_before_implementation(self) -> None:
        for operation in (PlanOperation.TEST, PlanOperation.VERIFY):
            with self.subTest(operation=operation):
                provider = FakeProvider()
                runtime = self.make_runtime(provider)
                plan = typed_plan([
                    AgentStep(
                        "step-1", "Run focused tests", operations=[operation],
                        expected_outcome="Tests pass",
                    ),
                ])
                result = await runtime.run_task(
                    AgentTask(TASK), TASK, self.session, self.repository,
                    model=MODEL, plan=plan,
                )
                self.assertEqual(
                    result.error.code, RuntimeErrorCode.VERIFICATION_NOT_IMPLEMENTED,
                )
                self.assertEqual(self.backend.calls, [])
                self.assertEqual(self.approvals, [])
                self.assertEqual(provider.requests, [])

    async def test_missing_explicit_required_output_fails_after_other_mutation(self) -> None:
        (self.root / "app" / "config.py").write_text("RETRIES = 2\n", encoding="utf-8")
        provider = FakeProvider(execution=[
            (
                self.patch_call(
                    "app/client.py",
                    "        return url",
                    "        return url + '!'",
                ),
                "Updated the client.",
            ),
            ([], "The step is complete."),
        ])
        runtime = self.make_runtime(provider)
        plan = typed_plan([
            AgentStep(
                "step-1",
                "Update client and required config output",
                paths=["app/client.py", "app/config.py"],
                operations=[PlanOperation.MODIFY],
                required_outputs=["app/config.py"],
                expected_outcome="The retry configuration is updated",
            ),
        ])

        result = await runtime.run_task(
            AgentTask(TASK), TASK, self.session, self.repository,
            model=MODEL, plan=plan,
        )

        self.assertFalse(result.ok)
        self.assertEqual(result.error.code, RuntimeErrorCode.EXPECTED_MUTATION_NOT_PERFORMED)
        self.assertIn("app/config.py", result.error.message)
        self.assertEqual(
            (self.root / "app" / "client.py").read_text(),
            "class Client:\n    def request(self, url):\n        return url + '!'\n",
        )

    async def test_pending_mutation_checkpoint_recovers_interrupted_without_replay(self) -> None:
        self.backend.block_mutation = True
        provider = FakeProvider(execution=[
            (
                self.patch_call("app/client.py", "        return url", "        return url + '!'"),
                "Patch the declared file.",
            ),
        ])
        runtime = self.make_runtime(provider)
        task = AgentTask(TASK)
        saved: list[dict[str, Any]] = []

        async def save(checkpoint: AgentCheckpoint) -> None:
            saved.append(checkpoint.to_dict())

        running = asyncio.create_task(runtime.run_task(
            task, TASK, self.session, self.repository,
            model=MODEL, checkpoint=save,
        ))
        for _ in range(100):
            if any(
                execution.get("status") == "pending"
                for data in saved
                for execution in data["task"]["executions"]
            ):
                break
            await asyncio.sleep(0.01)
        else:
            self.fail("No pending-mutation checkpoint was persisted")

        running.cancel()
        result = await running
        self.assertFalse(result.ok)
        pending = next(
            data for data in reversed(saved)
            if any(execution["status"] == "pending" for execution in data["task"]["executions"])
        )
        recovered = AgentCheckpoint.from_dict(pending)
        self.assertTrue(recovered.recover_interrupted())
        self.assertEqual(recovered.task.status, AgentStatus.INTERRUPTED)
        self.assertEqual(recovered.task.executions[0].status, ExecutionStatus.INTERRUPTED)
        self.assertEqual(recovered.task.plan.steps[0].status, StepStatus.INTERRUPTED)
        self.assertEqual(recovered.task.plan.steps[1].status, StepStatus.BLOCKED)
        self.assertFalse(recovered.recover_interrupted())
        self.backend.release_mutation.set()
        await asyncio.sleep(0)
        self.assertFalse((self.root / "app" / "client.py").read_text().endswith("!"))

    async def test_cancellation_before_planning_stops_without_provider_request(self) -> None:
        provider = FakeProvider()
        runtime = self.make_runtime(provider)
        cancellation = threading.Event()
        cancellation.set()
        result = await runtime.run_task(
            AgentTask(TASK), TASK, self.session, self.repository,
            model=MODEL, cancellation=cancellation,
        )
        self.assertEqual(result.state, AgentStatus.CANCELLED)
        self.assertEqual(result.error.code, RuntimeErrorCode.CANCELLED)
        self.assertEqual(provider.requests, [])

    async def test_read_only_task_can_finish_without_marking_implementation_verified(self) -> None:
        provider = FakeProvider(execution=[
            ([{"function": {
                "name": "read_file", "arguments": {"path": "app/client.py"},
            }}], "Read the source."),
            ([], "Current behavior is understood."),
        ])
        runtime = self.make_runtime(provider)
        plan = typed_plan([
            AgentStep(
                "step-1", "Inspect request behavior", paths=["app/client.py"],
                operations=[PlanOperation.READ],
                expected_outcome="Current behavior is understood",
            ),
        ], verification=[])
        result = await runtime.run_task(
            AgentTask(TASK), TASK, self.session, self.repository,
            model=MODEL, plan=plan,
        )
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.state, AgentStatus.COMPLETED)
        self.assertFalse(result.ready_for_verification)
        self.assertEqual(self.backend.calls, [("read_file", {"path": "app/client.py"}, None)])

    async def test_root_directory_listing_is_allowed_for_read_only_steps(self) -> None:
        provider = FakeProvider(execution=[
            ([{"function": {
                "name": "list_files", "arguments": {"path": "."},
            }}], "List the workspace root."),
            ([], "Workspace contents inspected."),
        ])
        runtime = self.make_runtime(provider)
        result = await runtime.run_task(
            AgentTask(TASK), TASK, self.session, self.repository, model=MODEL,
            plan=typed_plan([
                AgentStep(
                    "step-1", "Inspect top-level workspace",
                    operations=[PlanOperation.READ, PlanOperation.SEARCH],
                ),
            ], verification=[]),
        )
        self.assertTrue(result.ok, result.error)
        self.assertEqual(self.backend.calls[0][0:2], ("list_files", {"path": "."}))

    async def test_session_checkpoint_is_persisted_through_existing_history(self) -> None:
        history = History(Path(self.temp.name) / "history", self.settings)
        provider = FakeProvider(execution=[
            ([{"function": {
                "name": "read_file", "arguments": {"path": "app/client.py"},
            }}], "Read source."),
            ([], "Inspected."),
        ])
        runtime = self.make_runtime(provider, history=history)
        result = await runtime.run_task(
            AgentTask(TASK), TASK, self.session, self.repository, model=MODEL,
            plan=typed_plan([
                AgentStep(
                    "step-1", "Inspect source", paths=["app/client.py"],
                    operations=[PlanOperation.READ],
                ),
            ], verification=[]),
        )
        self.assertTrue(result.ok, result.error)
        self.assertEqual(self.session.schema_version, 6)
        self.assertIsNotNone(self.session.agent_checkpoint)
        restored = history.load(history.path_for(self.session.session_id))
        self.assertEqual(restored.agent_checkpoint.task.status, AgentStatus.COMPLETED)

    async def test_model_round_limit_fails_instead_of_assuming_step_completion(self) -> None:
        provider = FakeProvider(execution=[
            ([{"function": {
                "name": "read_file", "arguments": {"path": "app/client.py"},
            }}], "Read the source."),
        ])
        runtime = self.make_runtime(provider, limits=RuntimeLimits(max_rounds_per_step=1))
        result = await runtime.run_task(
            AgentTask(TASK), TASK, self.session, self.repository,
            model=MODEL,
            plan=typed_plan([
                AgentStep(
                    "step-1", "Inspect client", paths=["app/client.py"],
                    operations=[PlanOperation.READ],
                ),
            ], verification=[]),
        )
        self.assertEqual(result.error.code, RuntimeErrorCode.RESOURCE_LIMIT)
        self.assertEqual(self.backend.calls[0][0], "read_file")
        self.assertNotEqual(result.state, AgentStatus.COMPLETED)

    def test_runtime_limits_reject_unbounded_values(self) -> None:
        with self.assertRaisesRegex(ValueError, "hard safety bounds"):
            RuntimeLimits(max_tool_calls_per_task=5000)
        for invalid in (float("inf"), float("nan"), 86_401):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                RuntimeLimits(max_task_seconds=invalid)
