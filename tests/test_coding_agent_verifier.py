from __future__ import annotations

import asyncio
import hashlib
import json
import shlex
import threading
import tempfile
import threading
import unittest
from unittest.mock import patch
from dataclasses import replace
from pathlib import Path
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
    Repairability,
    StepStatus,
    VerificationEngine,
    VerificationIntent,
    VerificationLimits,
    VerificationOutcome,
    VerificationRequest,
    VerificationStatus,
)
from synai.coding_agent.repair import RepairController
from synai.config import ConversationEnvironment, Settings
from synai.intelligence import RepositoryIndex
from synai.models import ChatEvent, ModelInfo, Session
from synai.tools import Tools


MODEL = "verify-model"
GOAL = "Implement bounded retry handling and update tests."


class PlanProvider:
    def __init__(self) -> None:
        self.calls = 0
        self.repair_script: list[tuple[str, list[dict[str, Any]]]] = []
        self.requests: list[tuple[list[Any], list[dict[str, Any]]]] = []
        self.repair_started = asyncio.Event()
        self.repair_gate: asyncio.Event | None = None

    async def list_models(self) -> list[ModelInfo]:
        return [ModelInfo(MODEL, tools=True)]

    async def capabilities(self, name: str) -> ModelInfo:
        return ModelInfo(name, tools=True)

    async def chat(
        self,
        _model: str,
        _messages: list[Any],
        _tools: list[dict[str, Any]],
    ) -> AsyncIterator[ChatEvent]:
        del _model
        self.calls += 1
        self.requests.append((list(_messages), list(_tools)))
        if self.repair_script:
            self.repair_started.set()
            if self.repair_gate is not None:
                await self.repair_gate.wait()
            content, calls = self.repair_script.pop(0)
            yield ChatEvent(content=content, tool_calls=calls, done=True)
            return
        yield ChatEvent(content="", done=True)


class TerminalBackend:
    def __init__(self, workspace: Path, settings: Settings) -> None:
        self.workspace = workspace.resolve()
        self.settings = settings
        self.calls: list[dict[str, str]] = []
        self.outcomes: list[dict[str, Any]] = []
        self.wait_for_release: asyncio.Event | None = None
        self.started = asyncio.Event()

    def matches(self, session: Session) -> bool:
        return Path(session.workspace) == self.workspace

    async def execute(
        self,
        name: str,
        arguments: dict[str, Any],
        expected_sha256: str | None = None,
    ) -> dict[str, Any]:
        self.calls.append(dict(arguments))
        path = self.workspace / arguments.get("path", ".")
        if name == "preview":
            content = path.read_text(encoding="utf-8") if path.exists() else None
            digest = hashlib.sha256(content.encode()).hexdigest() if content is not None else None
            return {"ok": True, "content": content, "sha256": digest}
        if name == "patch_file":
            content = path.read_text(encoding="utf-8")
            if hashlib.sha256(content.encode()).hexdigest() != expected_sha256:
                return {"ok": False, "error": "File changed"}
            if not arguments["old"] or content.count(arguments["old"]) != 1:
                return {"ok": False, "error": "Patch must match exactly once"}
            self.started.set()
            if self.wait_for_release is not None:
                await self.wait_for_release.wait()
            path.write_text(content.replace(arguments["old"], arguments["new"], 1), encoding="utf-8")
            return {"ok": True, "path": arguments["path"]}
        if name != "terminal":
            raise AssertionError(f"Unexpected backend tool: {name}")
        self.started.set()
        if self.wait_for_release is not None:
            await self.wait_for_release.wait()
        if self.outcomes:
            return self.outcomes.pop(0)
        return {
            "ok": True,
            "stdout": "Ran successfully\n",
            "stderr": "",
            "exit_code": 0,
            "duration": 0.01,
            "timed_out": False,
            "truncated": False,
        }


class VerifierFixture(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / "project"
        self.root.mkdir()
        (self.root / "app").mkdir()
        (self.root / "tests").mkdir()
        (self.root / "app" / "client.py").write_text("def request():\n    return True\n")
        (self.root / "tests" / "test_client.py").write_text(
            "import unittest\n\n"
            "class RequestTests(unittest.TestCase):\n"
            "    def test_request(self):\n"
            "        self.assertTrue(True)\n"
        )
        (self.root / "pyproject.toml").write_text(
            "[project]\nname = 'fixture'\nversion = '0.1.0'\n"
        )
        self.settings = replace(
            Settings(execution_mode="host"),
            history_dir=self.base / ".synai",
            command_timeout=5,
            tool_budget=20,
        )
        self.backend = TerminalBackend(self.root, self.settings)
        self.session = Session(MODEL, self.settings.ollama_url, str(self.root))
        self.session.set_environment(
            ConversationEnvironment.from_settings(self.settings, self.root),
        )
        self.repository = RepositoryIndex(self.root)
        self.provider = PlanProvider()
        self.approvals: list[str] = []
        self.approve = True
        self.approval_started = asyncio.Event()
        self.approval_gate: asyncio.Event | None = None

        async def approval(name: str, _description: str) -> bool:
            del _description
            self.approvals.append(name)
            self.approval_started.set()
            if self.approval_gate is not None:
                await self.approval_gate.wait()
            return self.approve

        self.runtime = CodingAgentRuntime(
            self.provider,
            Tools(self.backend, approval),
        )

    def make_task(
        self,
        intents: list[VerificationIntent],
        *,
        paths: list[str] | None = None,
        context_hash: str = "0" * 64,
        record_change: bool = True,
    ) -> AgentTask:
        task_paths = paths or ["app/client.py", "tests/test_client.py"]
        step = AgentStep(
            "step-1",
            "Implement the planned retry behavior",
            purpose="Add bounded retries and its regression tests",
            paths=task_paths,
            operations=[PlanOperation.MODIFY],
            expected_outcome="Retry behavior is bounded",
            verification_criteria=["Run planned checks"],
        )
        step.status = StepStatus.COMPLETED
        task = AgentTask(GOAL, selected_model=MODEL)
        task.plan = AgentPlan(
            GOAL,
            [step],
            completion_criteria=["Retry behavior is bounded"],
            verification_intent=intents,
            executable_order=[step.step_id],
            context_hash=context_hash,
            planner_provider=type(self.provider).__name__,
            planner_model=MODEL,
            planning_attempts=1,
        )
        if record_change:
            task.executions.append(AgentExecution(
                step_id=step.step_id,
                tool_name="patch_file",
                status=ExecutionStatus.SUCCEEDED,
                operation=PlanOperation.MODIFY,
                target_path=task_paths[0],
                result_summary="Implementation path changed",
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

    def verify(self, task: AgentTask, **kwargs: Any):
        return self.runtime.run_verification(
            task, self.session, self.repository, **kwargs,
        )

    async def test_pass_runs_syntax_targeted_and_explicit_full_suite_then_review(self) -> None:
        task = self.make_task([
            VerificationIntent.TARGETED_TESTS,
            VerificationIntent.FULL_TEST_SUITE,
            VerificationIntent.SYNTAX_CHECK,
        ])
        checkpoints: list[dict[str, Any]] = []
        events = []

        async def checkpoint(value: AgentCheckpoint) -> None:
            checkpoints.append(value.to_dict())

        async def event_sink(value: Any) -> None:
            events.append(value)

        result = await self.verify(task, checkpoint=checkpoint, event_sink=event_sink)

        self.assertEqual(result.outcome, VerificationOutcome.PASSED)
        self.assertTrue(result.ready_for_review)
        self.assertFalse(result.repair_required)
        self.assertEqual(task.status, AgentStatus.REVIEWING)
        self.assertNotEqual(task.status, AgentStatus.COMPLETED)
        self.assertEqual(
            [call["command"] for call in self.backend.calls],
            [
                "python3 -m compileall -q -- app/client.py tests/test_client.py",
                "python3 -m unittest discover -s tests -p test_client.py",
                "python3 -m unittest discover -s tests",
            ],
        )
        self.assertEqual(self.approvals, ["terminal", "terminal", "terminal"])
        self.assertEqual([item.status for item in result.results], [
            VerificationStatus.PASSED,
            VerificationStatus.PASSED,
            VerificationStatus.PASSED,
        ])
        self.assertTrue(all(item.approval_state.value == "approved" for item in result.results))
        self.assertEqual(self.provider.calls, 0)
        self.assertTrue(any(
            event.kind == "ready_for_review" and event.state == AgentStatus.REVIEWING
            for event in events
        ))
        self.assertGreater(len(checkpoints), 3)

    async def test_test_failure_is_repair_candidate_without_mutation_or_model_call(self) -> None:
        self.backend.outcomes = [
            {
                "ok": False,
                "stdout": "FAIL: test_request (tests.test_client.RequestTests)\nAssertionError: expected true\n",
                "stderr": "",
                "exit_code": 1,
                "duration": 0.2,
                "timed_out": False,
                "truncated": False,
            },
        ]
        task = self.make_task([VerificationIntent.TARGETED_TESTS])

        result = await self.verify(task)

        self.assertEqual(result.outcome, VerificationOutcome.CODE_FAILURE)
        self.assertTrue(result.repair_required)
        self.assertEqual(result.state, AgentStatus.REPAIRING)
        self.assertEqual(result.results[0].status, VerificationStatus.FAILED)
        self.assertEqual(
            result.results[0].repairability,
            Repairability.CODE_REPAIR_CANDIDATE,
        )
        self.assertIn("FAIL:", result.results[0].failure_summary)
        self.assertEqual(self.provider.calls, 0)
        self.assertEqual([call["command"] for call in self.backend.calls], [
            "python3 -m unittest discover -s tests -p test_client.py",
        ])

    async def test_unconfigured_lint_is_blocked_not_code_failure(self) -> None:
        task = self.make_task([VerificationIntent.LINT])

        result = await self.verify(task)

        self.assertEqual(result.outcome, VerificationOutcome.BLOCKED)
        self.assertFalse(result.repair_required)
        self.assertEqual(result.state, AgentStatus.VERIFYING)
        self.assertEqual(result.results[0].status, VerificationStatus.UNAVAILABLE)
        self.assertIn("Ruff", result.results[0].infrastructure_error)
        self.assertEqual(self.backend.calls, [])

    async def test_infrastructure_block_takes_precedence_over_partial_code_failure(self) -> None:
        self.backend.outcomes = [{
            "ok": False,
            "stdout": "FAIL: test_request\nAssertionError: no\n",
            "stderr": "",
            "exit_code": 1,
            "duration": 0.1,
            "timed_out": False,
            "truncated": False,
        }]
        task = self.make_task([
            VerificationIntent.TARGETED_TESTS,
            VerificationIntent.LINT,
        ])
        result = await self.verify(task)
        self.assertEqual(result.outcome, VerificationOutcome.BLOCKED)
        self.assertFalse(result.repair_required)
        self.assertEqual(task.status, AgentStatus.VERIFYING)
        self.assertEqual(
            [item.status for item in result.results],
            [VerificationStatus.FAILED, VerificationStatus.UNAVAILABLE],
        )

    async def test_relevant_tests_use_path_evidence_but_targeted_needs_declared_test(self) -> None:
        task = self.make_task(
            [VerificationIntent.TARGETED_TESTS, VerificationIntent.RELEVANT_TESTS],
            paths=["app/client.py"],
        )

        result = await self.verify(task)

        self.assertEqual(
            [item.status for item in result.results],
            [VerificationStatus.UNAVAILABLE, VerificationStatus.PASSED],
        )
        self.assertIn("explicitly declared", result.results[0].infrastructure_error)
        self.assertEqual(result.results[1].relevant_paths, ("tests/test_client.py",))
        self.assertEqual(len(self.backend.calls), 1)
        self.assertEqual(
            self.backend.calls[0]["command"],
            "python3 -m unittest discover -s tests -p test_client.py",
        )

    async def test_syntax_failure_skips_expensive_full_suite_and_requires_repair(self) -> None:
        (self.root / "pyproject.toml").write_text(
            "[project]\nname = 'fixture'\n"
            "[tool.ruff]\nline-length = 88\n",
        )
        self.backend.outcomes = [{
            "ok": False,
            "stdout": "",
            "stderr": "app/client.py:1: SyntaxError: invalid syntax\n",
            "exit_code": 1,
            "duration": 0.1,
            "timed_out": False,
            "truncated": False,
        }]
        task = self.make_task([
            VerificationIntent.FULL_TEST_SUITE,
            VerificationIntent.SYNTAX_CHECK,
            VerificationIntent.LINT,
        ])

        result = await self.verify(task)

        self.assertEqual(result.outcome, VerificationOutcome.CODE_FAILURE)
        self.assertEqual(task.status, AgentStatus.REPAIRING)
        self.assertEqual(
            [item.status for item in result.results],
            [
                VerificationStatus.FAILED,
                VerificationStatus.PASSED,
                VerificationStatus.SKIPPED,
            ],
        )
        self.assertIn("Skipped after", result.results[2].failure_summary)
        self.assertEqual(len(self.backend.calls), 2)

    async def test_syntax_fail_fast_can_be_disabled_explicitly(self) -> None:
        self.backend.outcomes = [
            {
                "ok": False,
                "stdout": "",
                "stderr": "SyntaxError: invalid syntax\n",
                "exit_code": 1,
                "duration": 0.1,
                "timed_out": False,
                "truncated": False,
            },
            {
                "ok": True,
                "stdout": "",
                "stderr": "",
                "exit_code": 0,
                "duration": 0.1,
                "timed_out": False,
                "truncated": False,
            },
        ]
        task = self.make_task([
            VerificationIntent.SYNTAX_CHECK,
            VerificationIntent.FULL_TEST_SUITE,
        ])
        result = await self.verify(
            task,
            limits=VerificationLimits(fail_fast_on_syntax_failure=False),
        )
        self.assertEqual(
            [item.status for item in result.results],
            [VerificationStatus.FAILED, VerificationStatus.PASSED],
        )
        self.assertEqual(len(self.backend.calls), 2)

    async def test_no_verification_intent_is_not_a_fabricated_pass(self) -> None:
        task = self.make_task([], paths=["app/client.py"], record_change=False)
        task.plan.steps[0].operations = [PlanOperation.READ]
        task.plan.completion_criteria = []
        task.plan.validate()

        result = await self.verify(task)

        self.assertEqual(result.outcome, VerificationOutcome.NO_VERIFICATION_NEEDED)
        self.assertTrue(result.no_verification_needed)
        self.assertFalse(result.ready_for_review)
        self.assertEqual(result.results, ())
        self.assertEqual(task.status, AgentStatus.VERIFYING)
        self.assertEqual(self.backend.calls, [])

    async def test_symlink_target_is_rejected_before_verification_dispatch(self) -> None:
        (self.root / "app" / "linked.py").symlink_to(self.root / "app" / "client.py")
        task = self.make_task(
            [VerificationIntent.SYNTAX_CHECK],
            paths=["app/linked.py"],
        )
        task.executions[0].target_path = "app/linked.py"

        result = await self.verify(task)

        self.assertEqual(result.outcome, VerificationOutcome.ERROR)
        self.assertIn("Symlink", result.error)
        self.assertEqual(self.backend.calls, [])

    async def test_repair_snapshot_uses_safe_workspace_reader(self) -> None:
        controller = RepairController(self.runtime)
        expected = hashlib.sha256(
            (self.root / "app" / "client.py").read_bytes(),
        ).hexdigest()

        snapshots = controller._snapshots(
            self.session, self.repository, ("app/client.py", "app/missing.py"),
        )

        self.assertEqual(snapshots, {
            "app/client.py": expected,
            "app/missing.py": None,
        })

    async def test_repair_snapshot_rejects_file_deleted_during_capture(self) -> None:
        controller = RepairController(self.runtime)
        target = self.root / "app" / "client.py"
        original_read = self.repository.read_sources

        def delete_before_read(paths: tuple[str, ...], cancellation: Any = None):
            if target.exists():
                target.unlink()
            return original_read(paths, cancellation)

        with patch.object(
            self.repository, "read_sources", side_effect=delete_before_read,
        ):
            with self.assertRaisesRegex(OSError, "changed during capture"):
                controller._snapshots(
                    self.session, self.repository, ("app/client.py",),
                )

    async def test_repair_snapshot_rejects_file_replacement_during_safe_read(self) -> None:
        controller = RepairController(self.runtime)
        target = self.root / "app" / "client.py"
        original_read = self.repository.read_sources
        calls = 0

        def replace_after_first_read(paths: tuple[str, ...], cancellation: Any = None):
            nonlocal calls
            snapshots = original_read(paths, cancellation)
            calls += 1
            if calls == 1:
                target.write_text("def request():\n    return False\n", encoding="utf-8")
            return snapshots

        with patch.object(self.repository, "read_sources", side_effect=replace_after_first_read):
            with self.assertRaisesRegex(OSError, "changed during capture"):
                controller._snapshots(
                    self.session, self.repository, ("app/client.py",),
                )

    async def test_repair_snapshot_rejects_file_and_parent_symlinks(self) -> None:
        controller = RepairController(self.runtime)
        outside = self.base / "outside.py"
        outside.write_text("secret = True\n", encoding="utf-8")
        linked_file = self.root / "app" / "linked.py"
        linked_file.symlink_to(outside)
        with self.assertRaisesRegex(ValueError, "symlink"):
            controller._snapshots(
                self.session, self.repository, ("app/linked.py",),
            )

        app = self.root / "app"
        moved = self.root / "app-original"
        app.rename(moved)
        app.symlink_to(self.base, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "symlink"):
            controller._snapshots(
                self.session, self.repository, ("app/outside.py",),
            )

    async def test_repair_snapshot_rejects_oversized_nonregular_and_mismatched_workspace(self) -> None:
        controller = RepairController(self.runtime)
        target = self.root / "app" / "client.py"
        target.write_bytes(b"x" * (1_048_577))
        with self.assertRaisesRegex(OSError, "safe regular file"):
            controller._snapshots(
                self.session, self.repository, ("app/client.py",),
            )

        target.unlink()
        import os

        os.mkfifo(target)
        with self.assertRaisesRegex(ValueError, "regular"):
            controller._snapshots(
                self.session, self.repository, ("app/client.py",),
            )

        other = self.base / "other"
        other.mkdir()
        with self.assertRaisesRegex(Exception, "Workspace identity changed"):
            controller._snapshots(
                self.session, RepositoryIndex(other), ("app/client.py",),
            )

    async def test_terminal_approval_denial_blocks_without_dispatch_or_repair(self) -> None:
        self.approve = False
        task = self.make_task([VerificationIntent.TARGETED_TESTS])

        result = await self.verify(task)

        self.assertEqual(result.outcome, VerificationOutcome.BLOCKED)
        self.assertFalse(result.repair_required)
        self.assertEqual(task.status, AgentStatus.VERIFYING)
        self.assertEqual(result.results[0].status, VerificationStatus.BLOCKED)
        self.assertEqual(result.results[0].approval_state.value, "denied")
        self.assertEqual(self.backend.calls, [])
        self.assertEqual(self.approvals, ["terminal"])

    async def test_missing_pytest_module_is_verifier_unavailable(self) -> None:
        (self.root / "pytest.ini").write_text("[pytest]\n")
        self.backend.outcomes = [{
            "ok": False,
            "stdout": "",
            "stderr": "/usr/bin/python3: No module named pytest\n",
            "exit_code": 1,
            "duration": 0.1,
            "timed_out": False,
            "truncated": False,
        }]
        task = self.make_task([VerificationIntent.TARGETED_TESTS])

        result = await self.verify(task)

        self.assertEqual(result.results[0].status, VerificationStatus.UNAVAILABLE)
        self.assertEqual(result.outcome, VerificationOutcome.BLOCKED)
        self.assertFalse(result.repair_required)

    async def test_assertion_text_containing_missing_file_phrases_is_repairable(self) -> None:
        for message in (
            "AssertionError: Expected customer record not found",
            "AssertionError: no such file or directory",
        ):
            with self.subTest(message=message):
                self.backend.outcomes = [{
                    "ok": False,
                    "stdout": f"FAIL: test_client\n{message}\n",
                    "stderr": "",
                    "exit_code": 1,
                    "duration": 0.1,
                    "timed_out": False,
                    "truncated": False,
                }]
                result = await self.verify(self.make_task([VerificationIntent.TARGETED_TESTS]))

                self.assertEqual(result.results[0].status, VerificationStatus.FAILED)
                self.assertEqual(result.outcome, VerificationOutcome.CODE_FAILURE)
                self.assertEqual(
                    result.results[0].repairability,
                    Repairability.CODE_REPAIR_CANDIDATE,
                )

    async def test_missing_python_executable_is_unavailable(self) -> None:
        self.backend.outcomes = [{
            "ok": False,
            "stdout": "",
            "stderr": "/bin/sh: 1: python3: not found\n",
            "exit_code": 127,
            "duration": 0.01,
            "timed_out": False,
            "truncated": False,
        }]

        result = await self.verify(self.make_task([VerificationIntent.TARGETED_TESTS]))

        self.assertEqual(result.results[0].status, VerificationStatus.UNAVAILABLE)
        self.assertEqual(result.results[0].repairability, Repairability.NOT_REPAIRABLE)
        self.assertEqual(result.outcome, VerificationOutcome.BLOCKED)

    async def test_missing_project_dependency_is_unknown_not_repairable(self) -> None:
        self.backend.outcomes = [{
            "ok": False,
            "stdout": "",
            "stderr": "ModuleNotFoundError: No module named 'customer_sdk'\n",
            "exit_code": 1,
            "duration": 0.1,
            "timed_out": False,
            "truncated": False,
        }]

        task = self.make_task([VerificationIntent.TARGETED_TESTS])
        result = await self.verify(task)

        self.assertEqual(result.results[0].status, VerificationStatus.FAILED)
        self.assertEqual(result.results[0].repairability, Repairability.UNKNOWN)
        self.assertEqual(result.outcome, VerificationOutcome.BLOCKED)
        repair = await self.runtime.run_repair(task, self.session, self.repository)
        self.assertEqual(repair.outcome.value, "repair_blocked")
        self.assertEqual(self.provider.calls, 0)

    async def test_unrecognized_nonzero_exit_is_unknown(self) -> None:
        self.backend.outcomes = [{
            "ok": False,
            "stdout": "The runner stopped for an unclassified environment reason.\n",
            "stderr": "",
            "exit_code": 2,
            "duration": 0.1,
            "timed_out": False,
            "truncated": False,
        }]

        result = await self.verify(self.make_task([VerificationIntent.TARGETED_TESTS]))

        self.assertEqual(result.results[0].status, VerificationStatus.FAILED)
        self.assertEqual(result.results[0].repairability, Repairability.UNKNOWN)
        self.assertEqual(result.outcome, VerificationOutcome.BLOCKED)

    async def test_truncated_output_preserves_exit_status_but_not_repairability(self) -> None:
        self.backend.outcomes = [
            {
                "ok": False,
                "stdout": "passed " * 100,
                "stderr": "",
                "exit_code": 0,
                "duration": 0.1,
                "timed_out": False,
                "truncated": True,
            },
            {
                "ok": False,
                "stdout": "FAIL: test_client\nAssertionError: expected true\n" * 100,
                "stderr": "",
                "exit_code": 1,
                "duration": 0.1,
                "timed_out": False,
                "truncated": True,
            },
        ]
        task = self.make_task([
            VerificationIntent.TARGETED_TESTS,
            VerificationIntent.FULL_TEST_SUITE,
        ])
        result = await self.verify(task)

        self.assertEqual(
            [item.status for item in result.results],
            [VerificationStatus.PASSED, VerificationStatus.FAILED],
        )
        self.assertTrue(all(item.truncated for item in result.results))
        self.assertEqual(result.outcome, VerificationOutcome.BLOCKED)
        self.assertEqual(result.results[1].repairability, Repairability.UNKNOWN)
        self.assertFalse(result.repair_required)

    async def test_infrastructure_launch_failure_without_exit_code_is_blocked(self) -> None:
        self.backend.outcomes = [{
            "ok": False,
            "error": "Permission denied before terminal command launch",
            "duration": 0.01,
            "timed_out": False,
            "truncated": False,
        }]

        result = await self.verify(self.make_task([VerificationIntent.TARGETED_TESTS]))

        self.assertEqual(result.results[0].status, VerificationStatus.BLOCKED)
        self.assertEqual(result.results[0].repairability, Repairability.NOT_REPAIRABLE)
        self.assertEqual(result.outcome, VerificationOutcome.BLOCKED)

    async def test_timed_out_command_is_blocked_and_not_repairable(self) -> None:
        task = self.make_task([VerificationIntent.TARGETED_TESTS])
        self.backend.outcomes = [{
            "ok": False,
            "stdout": "partial",
            "stderr": "",
            "exit_code": -9,
            "duration": 0.05,
            "timed_out": True,
            "truncated": False,
        }]

        result = await self.verify(
            task, limits=VerificationLimits(max_command_seconds=0.05),
        )

        self.assertEqual(result.results[0].status, VerificationStatus.TIMED_OUT)
        self.assertEqual(result.outcome, VerificationOutcome.BLOCKED)
        self.assertEqual(result.results[0].repairability, Repairability.NOT_REPAIRABLE)
        self.assertFalse(result.repair_required)

    async def test_generated_paths_are_shell_quoted_and_output_is_bounded(self) -> None:
        malicious = "app/client;touch PWN.py"
        (self.root / malicious).write_text("x = 1\n")
        task = self.make_task(
            [VerificationIntent.SYNTAX_CHECK],
            paths=[malicious],
        )
        self.backend.outcomes = [{
            "ok": True,
            "stdout": "é" * 20_000,
            "stderr": "界" * 20_000,
            "exit_code": 0,
            "duration": 0.1,
            "timed_out": False,
            "truncated": False,
        }]

        result = await self.verify(
            task,
            limits=VerificationLimits(
                max_stdout_bytes=128,
                max_stderr_bytes=128,
                max_check_output_bytes=256,
                max_total_output_bytes=256,
            ),
        )

        command = self.backend.calls[0]["command"]
        self.assertIn(shlex.quote(malicious), command)
        self.assertEqual(shlex.split(command), list(result.plan.checks[0].argv))
        self.assertNotIn("PWN", [item.name for item in self.root.iterdir()])
        self.assertTrue(result.results[0].truncated)
        self.assertLessEqual(len(result.results[0].stdout.encode()), 128)
        self.assertLessEqual(len(json.dumps(task.to_dict()).encode()), 500_000)
        self.assertLessEqual(len(result.results[0].stderr.encode()), 128)

    async def test_nested_project_selects_only_changed_backend_project(self) -> None:
        backend = self.root / "backend"
        frontend = self.root / "frontend"
        (backend / "tests").mkdir(parents=True)
        (frontend / "src").mkdir(parents=True)
        (backend / "app.py").write_text("value = 1\n")
        (backend / "tests" / "test_app.py").write_text("def test_app():\n    assert True\n")
        (backend / "pyproject.toml").write_text("[project]\nname='backend'\n")
        (frontend / "package.json").write_text(json.dumps({
            "name": "front", "scripts": {"test": "jest"},
            "packageManager": "npm@10",
        }))
        (frontend / "package-lock.json").write_text("{}")
        self.backend.workspace = self.root.resolve()
        task = self.make_task(
            [VerificationIntent.TARGETED_TESTS],
            paths=["backend/app.py", "backend/tests/test_app.py"],
        )
        task.executions[0].target_path = "backend/app.py"
        task.plan.steps[0].paths = ["backend/app.py", "backend/tests/test_app.py"]
        task.plan.validate()

        generated = VerificationEngine(self.runtime.tools).build_plan(
            VerificationRequest(task, self.session, self.repository),
        )
        self.assertEqual(generated.project_type, "python")
        self.assertEqual(generated.project_roots, ("backend",))
        self.assertTrue(all(check.cwd == "backend" for check in generated.checks))
        self.assertNotIn("frontend", json.dumps(generated.to_dict()))

    async def test_checkpoint_recovery_marks_running_check_interrupted_without_replay(self) -> None:
        task = self.make_task([VerificationIntent.TARGETED_TESTS])
        pending_checkpoints: list[dict[str, Any]] = []
        self.backend.wait_for_release = asyncio.Event()
        cancellation = threading.Event()

        async def save(checkpoint: AgentCheckpoint) -> None:
            data = checkpoint.to_dict()
            if any(
                item["status"] == VerificationStatus.RUNNING.value
                for item in data["task"]["verification_results"]
            ):
                pending_checkpoints.append(data)

        verification = asyncio.create_task(self.verify(
            task,
            checkpoint=save,
            cancellation=cancellation,
        ))
        await asyncio.wait_for(self.backend.started.wait(), timeout=1)
        cancellation.set()
        result = await verification

        self.assertEqual(result.outcome, VerificationOutcome.CANCELLED)
        self.assertEqual(task.status, AgentStatus.CANCELLED)
        self.assertEqual(task.verification_results[0].status, VerificationStatus.INTERRUPTED)
        self.assertEqual(len(self.backend.calls), 1)
        self.assertTrue(pending_checkpoints)
        restored = AgentCheckpoint.from_dict(pending_checkpoints[-1])
        self.assertTrue(restored.recover_interrupted())
        self.assertEqual(restored.task.status, AgentStatus.INTERRUPTED)
        self.assertEqual(
            restored.task.verification_results[0].status,
            VerificationStatus.INTERRUPTED,
        )
        replay = await self.verify(restored.task)
        self.assertEqual(replay.outcome, VerificationOutcome.ERROR)
        self.assertEqual(len(self.backend.calls), 1)
        self.backend.wait_for_release = None
        restarted = await self.runtime.run_verification(
            restored.task,
            self.session,
            self.repository,
            restart_interrupted=True,
        )
        self.assertEqual(restarted.outcome, VerificationOutcome.PASSED)
        self.assertEqual(restored.task.status, AgentStatus.REVIEWING)
        self.assertEqual(
            [item.status for item in restarted.results],
            [VerificationStatus.INTERRUPTED, VerificationStatus.PASSED],
        )
        self.assertNotEqual(restarted.results[0].run_id, restarted.results[1].run_id)
        self.assertEqual(len(self.backend.calls), 2)

    async def test_node_manifest_scripts_are_selected_only_when_configured(self) -> None:
        frontend = self.root / "frontend"
        (frontend / "src").mkdir(parents=True)
        (frontend / "tests").mkdir()
        (frontend / "src" / "client.ts").write_text("export const value = 1;\n")
        (frontend / "tests" / "client.test.ts").write_text("test('client', () => {});\n")
        (frontend / "package.json").write_text(json.dumps({
            "name": "front",
            "scripts": {"test": "vitest", "lint": "eslint .", "build": "vite build"},
            "packageManager": "npm@10",
            "devDependencies": {"typescript": "^5"},
        }))
        (frontend / "package-lock.json").write_text("{}")
        (frontend / "tsconfig.json").write_text("{}")
        task = self.make_task(
            [
                VerificationIntent.TARGETED_TESTS,
                VerificationIntent.LINT,
                VerificationIntent.TYPE_CHECK,
                VerificationIntent.BUILD,
            ],
            paths=["frontend/src/client.ts", "frontend/tests/client.test.ts"],
        )
        task.executions[0].target_path = "frontend/src/client.ts"
        task.plan.steps[0].paths = [
            "frontend/src/client.ts",
            "frontend/tests/client.test.ts",
        ]
        task.plan.validate()
        generated = VerificationEngine(self.runtime.tools).build_plan(
            VerificationRequest(task, self.session, self.repository),
        )
        self.assertEqual(generated.project_type, "node")
        self.assertEqual(
            [check.argv[0:3] for check in generated.checks if check.available],
            [
                ("npm", "run", "test"),
                ("npm", "run", "lint"),
                ("node_modules/.bin/tsc", "--noEmit"),
                ("npm", "run", "build"),
            ],
        )
        self.assertTrue(all(check.cwd == "frontend" for check in generated.checks))

    async def test_rust_checks_require_explicit_intents(self) -> None:
        (self.root / "pyproject.toml").unlink()
        (self.root / "app").rename(self.root / "src")
        (self.root / "Cargo.toml").write_text(
            "[workspace]\nmembers = []\nresolver = '2'\n"
        )
        task = self.make_task([
            VerificationIntent.FULL_TEST_SUITE,
            VerificationIntent.TYPE_CHECK,
            VerificationIntent.LINT,
        ], paths=["src/client.py"])
        task.executions[0].target_path = "src/client.py"
        task.plan.steps[0].paths = ["src/client.py"]
        task.plan.validate()
        generated = VerificationEngine(self.runtime.tools).build_plan(
            VerificationRequest(task, self.session, self.repository),
        )
        self.assertEqual(generated.project_type, "rust")
        self.assertEqual([check.argv[:2] for check in generated.checks], [
            ("cargo", "clippy"),
            ("cargo", "check"),
            ("cargo", "test"),
        ])

    async def test_invalid_task_state_and_workspace_mismatch_never_dispatch(self) -> None:
        task = self.make_task([VerificationIntent.TARGETED_TESTS])
        task.status = AgentStatus.IMPLEMENTING
        result = await self.verify(task)
        self.assertEqual(result.outcome, VerificationOutcome.ERROR)
        self.assertEqual(self.backend.calls, [])
        task = self.make_task([VerificationIntent.TARGETED_TESTS])
        self.backend.workspace = self.base.resolve()
        result = await self.verify(task)
        self.assertEqual(result.outcome, VerificationOutcome.ERROR)
        self.assertEqual(self.backend.calls, [])

    async def test_checkpoint_failure_stops_before_dispatch_and_is_reported(self) -> None:
        task = self.make_task([VerificationIntent.TARGETED_TESTS])

        async def fail_checkpoint(_checkpoint: AgentCheckpoint) -> None:
            raise OSError("history unavailable")

        result = await self.verify(task, checkpoint=fail_checkpoint)
        self.assertEqual(result.outcome, VerificationOutcome.ERROR)
        self.assertIn("checkpoint failed", result.error)
        self.assertEqual(task.verification_outcome, VerificationOutcome.ERROR)
        self.assertEqual(self.backend.calls, [])

    async def test_python_type_checker_requires_explicit_project_evidence(self) -> None:
        (self.root / "pyproject.toml").write_text(
            "[project]\nname = 'fixture'\n"
            "[tool.mypy]\nstrict = true\n",
        )
        task = self.make_task(
            [VerificationIntent.TYPE_CHECK],
            paths=["app/client.py"],
        )
        generated = VerificationEngine(self.runtime.tools).build_plan(
            VerificationRequest(task, self.session, self.repository),
        )
        check, = generated.checks
        self.assertTrue(check.available)
        self.assertEqual(check.argv, ("mypy", "--", "app/client.py"))

    async def test_unsupported_project_intent_is_blocked_not_guessed(self) -> None:
        (self.root / "pyproject.toml").unlink()
        task = self.make_task(
            [VerificationIntent.LINT],
            paths=["README.md"],
        )
        result = await self.verify(task)
        self.assertEqual(result.outcome, VerificationOutcome.BLOCKED)
        self.assertEqual(result.results[0].status, VerificationStatus.UNAVAILABLE)
        self.assertEqual(self.backend.calls, [])

    async def test_metadata_reader_rejects_oversized_symlink_and_invalid_utf8(self) -> None:
        from synai.coding_agent.verifier import _read_workspace_text

        metadata = self.root / "metadata.txt"
        metadata.write_text("bounded")
        self.assertIsNone(_read_workspace_text(self.root, "metadata.txt", 3))
        self.assertEqual(_read_workspace_text(self.root, "metadata.txt", 16), "bounded")
        (self.root / "outside.txt").write_text("outside")
        (self.root / "link.txt").symlink_to(self.root / "outside.txt")
        self.assertIsNone(_read_workspace_text(self.root, "link.txt", 16))
        (self.root / "invalid.txt").write_bytes(b"\xff")
        self.assertIsNone(_read_workspace_text(self.root, "invalid.txt", 16))

    def _repair_script(self, targets: list[str], old: str, new: str) -> None:
        diagnosis = json.dumps({
            "diagnosis": "The implementation has the verified failing behavior.",
            "intended_targets": targets,
            "intended_symbols": ["request"],
            "action_summary": "Apply the smallest implementation correction.",
            "uncertainty": "The verifier output is bounded.",
            "scope_sufficient": True,
        })
        patch = [{
            "function": {
                "name": "patch_file",
                "arguments": {"path": targets[0], "old": old, "new": new},
            },
        }]
        self.provider.repair_script.extend([
            (diagnosis, []),
            ("Apply the focused patch.", patch),
            ("The in-scope repair mutation is complete.", []),
        ])

    @staticmethod
    def _verification_result(ok: bool) -> dict[str, Any]:
        return {
            "ok": ok,
            "stdout": "" if ok else "FAIL: test_request\nAssertionError: expected retry\n",
            "stderr": "",
            "exit_code": 0 if ok else 1,
            "duration": 0.1,
            "timed_out": False,
            "truncated": False,
        }

    async def test_repair_mutation_reuses_verifier_and_passes(self) -> None:
        self.backend.outcomes = [
            self._verification_result(False),
            self._verification_result(True),
        ]
        task = self.make_task([VerificationIntent.TARGETED_TESTS])
        initial = await self.verify(task)
        self.assertEqual(initial.outcome, VerificationOutcome.CODE_FAILURE)
        self._repair_script(["app/client.py"], "return True", "return False")

        result = await self.runtime.run_repair(
            task, self.session, self.repository,
        )

        self.assertEqual(result.outcome.value, "verification_passed", result.error)
        self.assertEqual(task.status, AgentStatus.REVIEWING)
        self.assertEqual(len(task.repair_attempts), 1)
        attempt = task.repair_attempts[0]
        self.assertEqual(attempt.attempt, 1)
        self.assertEqual(attempt.triggering_check_id, initial.results[0].check_id)
        self.assertEqual(attempt.mutated_paths, ("app/client.py",))
        self.assertTrue(attempt.execution_ids)
        self.assertIsNotNone(attempt.next_verification_run_id)
        self.assertEqual(
            [item.status for item in task.verification_results],
            [VerificationStatus.FAILED, VerificationStatus.PASSED],
        )
        self.assertEqual(
            [item.run_id for item in task.verification_results][0],
            attempt.triggering_run_id,
        )
        self.assertNotEqual(
            task.verification_results[-1].run_id,
            attempt.triggering_run_id,
        )
        self.assertEqual(self.provider.calls, 3)
        self.assertIn("no markdown or chain-of-thought", self.provider.requests[0][0][0].content)
        self.assertIn("repair_allowed_mutation_paths", self.provider.requests[1][0][-1].content)

    async def test_configuration_repair_is_allowed_only_for_declared_config_path(self) -> None:
        self.backend.outcomes = [
            self._verification_result(False),
            self._verification_result(True),
        ]
        task = self.make_task(
            [VerificationIntent.TARGETED_TESTS],
            paths=["app/client.py", "tests/test_client.py", "pyproject.toml"],
        )
        initial = await self.verify(task)
        initial.results[0].repairability = Repairability.CONFIGURATION_REPAIR_CANDIDATE
        (self.root / "pyproject.toml").write_text(
            "[project]\nname = 'fixture'\nversion = '0.1.0'\n# bad setting\n",
        )
        old = "# bad setting"
        new = "[tool.pytest.ini_options]\n"
        diagnosis = json.dumps({
            "diagnosis": "The authorized project configuration causes the failure.",
            "intended_targets": ["pyproject.toml"],
            "intended_symbols": [],
            "action_summary": "Correct the declared configuration.",
            "uncertainty": "The exact verifier limitation is retained.",
            "scope_sufficient": True,
        })
        self.provider.repair_script.extend([
            (diagnosis, []),
            ("Patch the declared configuration.", [{
                "function": {
                    "name": "patch_file",
                    "arguments": {"path": "pyproject.toml", "old": old, "new": new},
                },
            }]),
            ("The configuration edit is complete.", []),
        ])

        result = await self.runtime.run_repair(task, self.session, self.repository)

        self.assertEqual(result.outcome.value, "verification_passed", result.error)
        self.assertEqual(task.repair_attempts[0].mutated_paths, ("pyproject.toml",))

    async def test_repair_does_not_start_when_code_failure_is_not_repairable(self) -> None:
        self.backend.outcomes = [self._verification_result(False)]
        task = self.make_task([VerificationIntent.TARGETED_TESTS])
        initial = await self.verify(task)
        initial.results[0].repairability = Repairability.NOT_REPAIRABLE

        result = await self.runtime.run_repair(task, self.session, self.repository)

        self.assertEqual(result.outcome.value, "repair_blocked", result.error)
        self.assertEqual(task.status, AgentStatus.FAILED)
        self.assertEqual(self.provider.calls, 0)
        self.assertEqual(len(task.repair_attempts), 0)

    async def test_repair_noop_mutation_does_not_rerun_verification(self) -> None:
        self.backend.outcomes = [
            self._verification_result(False),
        ]
        task = self.make_task([VerificationIntent.TARGETED_TESTS])
        await self.verify(task)
        self._repair_script(["app/client.py"], "return True", "return True")

        result = await self.runtime.run_repair(task, self.session, self.repository)

        self.assertEqual(result.outcome.value, "repair_mutation_not_performed", result.error)
        self.assertEqual(task.status, AgentStatus.FAILED)
        self.assertTrue(task.repair_attempts[0].no_progress)
        self.assertEqual(len(task.verification_results), 1)

    async def test_repair_approval_denial_stops_without_retrying_mutation(self) -> None:
        self.backend.outcomes = [
            self._verification_result(False),
        ]
        task = self.make_task([VerificationIntent.TARGETED_TESTS])
        await self.verify(task)
        self.approve = False
        self._repair_script(["app/client.py"], "return True", "return False")

        result = await self.runtime.run_repair(task, self.session, self.repository)

        self.assertEqual(result.outcome.value, "repair_blocked")
        self.assertEqual(task.status, AgentStatus.FAILED)
        self.assertEqual(self.approvals.count("patch_file"), 1)
        self.assertEqual(self.provider.calls, 2)
        self.assertEqual(task.repair_attempts[0].mutated_paths, ())
        self.assertEqual(len(task.verification_results), 1)

    async def test_out_of_scope_diagnosis_requires_replan_before_mutation(self) -> None:
        self.backend.outcomes = [self._verification_result(False)]
        task = self.make_task(
            [VerificationIntent.TARGETED_TESTS],
        )
        task.goal = "Correct request handling."
        task.plan.goal = task.goal
        task.plan.steps[0].description = "Correct the request implementation"
        task.plan.steps[0].purpose = "Handle the verified request failure"
        task.plan.validate()
        await self.verify(task)
        self._repair_script(["tests/test_client.py"], "return True", "return False")

        result = await self.runtime.run_repair(task, self.session, self.repository)

        self.assertEqual(result.outcome.value, "replan_required", result.error)
        self.assertEqual(task.status, AgentStatus.FAILED)
        self.assertEqual(self.provider.calls, 1)
        self.assertEqual(len(task.repair_attempts[0].execution_ids), 0)

    async def test_repair_attempt_limit_is_application_enforced(self) -> None:
        self.backend.outcomes = [
            self._verification_result(False),
            self._verification_result(False),
        ]
        task = self.make_task([VerificationIntent.TARGETED_TESTS])
        await self.verify(task)
        self._repair_script(["app/client.py"], "return True", "return False")

        result = await self.runtime.run_repair(
            task, self.session, self.repository, max_attempts=1,
        )

        self.assertEqual(result.outcome.value, "repair_attempts_exhausted")
        self.assertEqual(task.status, AgentStatus.FAILED)
        self.assertEqual(len(task.repair_attempts), 1)
        self.assertEqual(self.provider.calls, 3)
        self.assertEqual(len(task.verification_results), 2)

    async def test_second_repair_uses_latest_failure_and_repeats_are_recorded(self) -> None:
        self.backend.outcomes = [
            self._verification_result(False),
            self._verification_result(False),
            self._verification_result(True),
        ]
        task = self.make_task([VerificationIntent.TARGETED_TESTS])
        initial = await self.verify(task)
        self._repair_script(["app/client.py"], "return True", "return False")
        self._repair_script(["app/client.py"], "return False", "return True")

        result = await self.runtime.run_repair(task, self.session, self.repository)

        self.assertEqual(result.outcome.value, "verification_passed")
        self.assertEqual([attempt.attempt for attempt in task.repair_attempts], [1, 2])
        self.assertTrue(task.repair_attempts[1].repeated_failure)
        self.assertEqual(
            task.repair_attempts[1].triggering_run_id,
            task.verification_results[-2].run_id,
        )
        self.assertNotEqual(
            task.repair_attempts[1].triggering_run_id,
            initial.results[0].run_id,
        )
        self.assertEqual(self.provider.calls, 6)

    async def test_blocked_verification_after_repair_stops_without_another_attempt(self) -> None:
        self.backend.outcomes = [
            self._verification_result(False),
            {
                "ok": False,
                "stdout": "",
                "stderr": "execution timed out",
                "exit_code": None,
                "duration": 5.0,
                "timed_out": True,
                "truncated": False,
            },
        ]
        task = self.make_task([VerificationIntent.TARGETED_TESTS])
        await self.verify(task)
        self._repair_script(["app/client.py"], "return True", "return False")

        result = await self.runtime.run_repair(task, self.session, self.repository)

        self.assertEqual(result.outcome.value, "verification_blocked")
        self.assertEqual(task.status, AgentStatus.VERIFYING)
        self.assertEqual(task.repair_outcome.value, "verification_blocked")
        self.assertEqual(len(task.repair_attempts), 1)
        self.assertEqual(self.provider.calls, 3)
        self.assertEqual(task.verification_results[-1].status, VerificationStatus.TIMED_OUT)

    async def test_cancellation_before_repair_starts_no_provider_call(self) -> None:
        self.backend.outcomes = [self._verification_result(False)]
        task = self.make_task([VerificationIntent.TARGETED_TESTS])
        await self.verify(task)
        cancellation = threading.Event()
        cancellation.set()

        result = await self.runtime.run_repair(
            task,
            self.session,
            self.repository,
            cancellation=cancellation,
        )

        self.assertEqual(result.outcome.value, "repair_cancelled")
        self.assertEqual(task.status, AgentStatus.CANCELLED)
        self.assertEqual(self.provider.calls, 0)
        self.assertEqual(task.repair_attempts, [])

    async def test_cancellation_during_repair_diagnosis_stops_provider_loop(self) -> None:
        self.backend.outcomes = [self._verification_result(False)]
        task = self.make_task([VerificationIntent.TARGETED_TESTS])
        await self.verify(task)
        self._repair_script(["app/client.py"], "return True", "return False")
        self.provider.repair_gate = asyncio.Event()
        cancellation = threading.Event()
        running = asyncio.create_task(self.runtime.run_repair(
            task, self.session, self.repository, cancellation=cancellation,
        ))

        await asyncio.wait_for(self.provider.repair_started.wait(), timeout=1)
        cancellation.set()
        result = await asyncio.wait_for(running, timeout=1)

        self.assertEqual(result.outcome.value, "repair_cancelled")
        self.assertEqual(task.status, AgentStatus.CANCELLED)
        self.assertEqual(self.provider.calls, 1)
        self.assertEqual(len(task.repair_attempts), 1)
        self.assertEqual(task.repair_attempts[0].status.value, "cancelled")

    async def test_cancellation_during_mutation_approval_stops_without_dispatch(self) -> None:
        self.backend.outcomes = [self._verification_result(False)]
        task = self.make_task([VerificationIntent.TARGETED_TESTS])
        await self.verify(task)
        self._repair_script(["app/client.py"], "return True", "return False")
        self.approval_gate = asyncio.Event()
        self.approval_started = asyncio.Event()
        cancellation = threading.Event()
        running = asyncio.create_task(self.runtime.run_repair(
            task, self.session, self.repository, cancellation=cancellation,
        ))

        await asyncio.wait_for(self.approval_started.wait(), timeout=1)
        cancellation.set()
        result = await asyncio.wait_for(running, timeout=1)

        self.assertEqual(result.outcome.value, "repair_cancelled")
        self.assertEqual(task.status, AgentStatus.CANCELLED)
        self.assertEqual(self.approvals.count("patch_file"), 1)
        self.assertTrue(task.repair_attempts[0].execution_ids)
        self.assertEqual(
            task.executions[-1].status.value,
            "interrupted",
        )

    async def test_cancellation_after_dispatch_keeps_execution_and_skips_verification(self) -> None:
        self.backend.outcomes = [self._verification_result(False)]
        task = self.make_task([VerificationIntent.TARGETED_TESTS])
        await self.verify(task)
        self._repair_script(["app/client.py"], "return True", "return False")
        self.backend.wait_for_release = asyncio.Event()
        self.backend.started = asyncio.Event()
        cancellation = threading.Event()
        running = asyncio.create_task(self.runtime.run_repair(
            task, self.session, self.repository, cancellation=cancellation,
        ))

        await asyncio.wait_for(self.backend.started.wait(), timeout=1)
        cancellation.set()
        result = await asyncio.wait_for(running, timeout=1)

        self.assertEqual(result.outcome.value, "repair_cancelled")
        self.assertEqual(task.status, AgentStatus.CANCELLED)
        self.assertTrue(task.repair_attempts[0].execution_ids)
        self.assertEqual(task.executions[-1].status.value, "interrupted")
        self.assertEqual(len(task.verification_results), 1)
        self.assertEqual(
            (self.root / "app" / "client.py").read_text(),
            "def request():\n    return True\n",
        )

    async def test_latest_nonrepairable_failure_blocks_earlier_repairable_failure(self) -> None:
        self.backend.outcomes = [
            {
                "ok": False,
                "stdout": "",
                "stderr": "app/client.py:1: SyntaxError: invalid syntax\n",
                "exit_code": 1,
                "duration": 0.1,
                "timed_out": False,
                "truncated": False,
            },
            self._verification_result(False),
        ]
        task = self.make_task([
            VerificationIntent.SYNTAX_CHECK,
            VerificationIntent.TARGETED_TESTS,
        ])
        initial = await self.verify(
            task,
            limits=VerificationLimits(fail_fast_on_syntax_failure=False),
        )
        self.assertEqual(initial.outcome, VerificationOutcome.CODE_FAILURE)
        failed_results = [
            item for item in initial.results
            if item.status == VerificationStatus.FAILED
        ]
        self.assertGreaterEqual(len(failed_results), 2)
        failed_results[-1].repairability = Repairability.NOT_REPAIRABLE

        result = await self.runtime.run_repair(task, self.session, self.repository)

        self.assertEqual(result.outcome.value, "repair_blocked")
        self.assertEqual(self.provider.calls, 0)
        self.assertEqual(task.repair_attempts, [])

    async def test_configuration_candidate_requires_declared_configuration_path(self) -> None:
        self.backend.outcomes = [self._verification_result(False)]
        task = self.make_task(
            [VerificationIntent.TARGETED_TESTS],
            paths=["app/client.py", "tests/test_client.py"],
        )
        await self.verify(task)
        task.verification_results[-1].repairability = Repairability.CONFIGURATION_REPAIR_CANDIDATE

        result = await self.runtime.run_repair(task, self.session, self.repository)

        self.assertEqual(result.outcome.value, "replan_required", result.error)
        self.assertEqual(self.provider.calls, 0)
        self.assertEqual(task.repair_attempts[0].intended_targets, ())

    async def test_test_mutation_requires_original_task_and_plan_intent(self) -> None:
        self.backend.outcomes = [self._verification_result(False)]
        task = self.make_task([VerificationIntent.TARGETED_TESTS])
        task.goal = "Correct request behavior."
        task.plan.goal = task.goal
        task.plan.steps[0].description = "Correct the request implementation"
        task.plan.steps[0].purpose = "Handle the verified failure"
        task.plan.validate()
        await self.verify(task)
        self._repair_script(["tests/test_client.py"], "assertTrue(True)", "assertTrue(False)")

        result = await self.runtime.run_repair(task, self.session, self.repository)

        self.assertEqual(result.outcome.value, "replan_required")
        self.assertEqual(self.provider.calls, 1)
        self.assertEqual(self.approvals.count("patch_file"), 0)

    async def test_malformed_repair_diagnosis_is_provider_error(self) -> None:
        self.backend.outcomes = [self._verification_result(False)]
        task = self.make_task([VerificationIntent.TARGETED_TESTS])
        await self.verify(task)
        self.provider.repair_script.append(("not JSON", []))

        result = await self.runtime.run_repair(task, self.session, self.repository)

        self.assertEqual(result.outcome.value, "repair_provider_error")
        self.assertEqual(self.provider.calls, 1)
        self.assertEqual(task.status, AgentStatus.FAILED)
        self.assertEqual(task.repair_attempts[0].status.value, "failed")

    async def test_malformed_repair_tool_call_is_corrected_within_one_attempt(self) -> None:
        self.backend.outcomes = [
            self._verification_result(False),
            self._verification_result(True),
        ]
        task = self.make_task([VerificationIntent.TARGETED_TESTS])
        await self.verify(task)
        diagnosis = json.dumps({
            "diagnosis": "The source implementation has the verified failure.",
            "intended_targets": ["app/client.py"],
            "intended_symbols": ["request"],
            "action_summary": "Patch the return behavior.",
            "uncertainty": "Low.",
            "scope_sufficient": True,
        })
        self.provider.repair_script.extend([
            (diagnosis, []),
            ("Malformed initial patch arguments.", [{
                "function": {"name": "patch_file", "arguments": "{"},
            }]),
            ("Corrected patch arguments.", [{
                "function": {
                    "name": "patch_file",
                    "arguments": {
                        "path": "app/client.py",
                        "old": "return True",
                        "new": "return False",
                    },
                },
            }]),
            ("The repair mutation is complete.", []),
        ])

        result = await self.runtime.run_repair(task, self.session, self.repository)

        self.assertEqual(result.outcome.value, "verification_passed", result.error)
        self.assertEqual(len(task.repair_attempts), 1)
        self.assertEqual(self.provider.calls, 4)

    async def test_stale_patch_is_corrected_within_one_attempt(self) -> None:
        self.backend.outcomes = [
            self._verification_result(False),
            self._verification_result(True),
        ]
        task = self.make_task([VerificationIntent.TARGETED_TESTS])
        await self.verify(task)
        self._repair_script(["app/client.py"], "obsolete text", "replacement")
        corrected_patch = [{
            "function": {
                "name": "patch_file",
                "arguments": {
                    "path": "app/client.py",
                    "old": "return True",
                    "new": "return False",
                },
            },
        }]
        self.provider.repair_script.insert(
            len(self.provider.repair_script) - 1,
            ("The source excerpt was stale; retry the exact current line.", corrected_patch),
        )

        result = await self.runtime.run_repair(task, self.session, self.repository)

        self.assertEqual(result.outcome.value, "verification_passed", result.error)
        self.assertEqual(len(task.repair_attempts), 1)
        self.assertEqual(self.provider.calls, 4)
        repair_execution_ids = set(task.repair_attempts[0].execution_ids)
        self.assertEqual(
            [
                execution.status.value for execution in task.executions
                if execution.execution_id in repair_execution_ids
            ],
            ["failed", "succeeded"],
        )

    async def test_attempt_deadline_bounds_event_and_checkpoint_work(self) -> None:
        self.backend.outcomes = [self._verification_result(False)]
        task = self.make_task([VerificationIntent.TARGETED_TESTS])
        await self.verify(task)

        async def slow_attempt_event(event: Any) -> None:
            if event.kind == "repair_attempt_started":
                await asyncio.sleep(0.05)

        result = await self.runtime.run_repair(
            task,
            self.session,
            self.repository,
            event_sink=slow_attempt_event,
            max_attempt_seconds=0.005,
        )

        self.assertEqual(result.outcome.value, "repair_resource_limit")
        self.assertEqual(task.status, AgentStatus.FAILED)
        self.assertEqual(self.provider.calls, 0)
