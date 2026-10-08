from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import asdict, replace
from pathlib import Path
from unittest.mock import AsyncMock

from support import bind_sandbox

from synai.agent import Agent, SYSTEM
from synai.coding_agent.state import (
    AgentCheckpoint,
    AgentExecution,
    AgentPlan,
    AgentStatus,
    AgentStep,
    AgentTask,
    ExecutionStatus,
    RepairAttempt,
    RepairOutcome,
    RepairStatus,
    StepStatus,
    VerificationResult,
    VerificationStatus,
)
from synai.config import ConversationEnvironment, Settings
from synai.history import History, HistoryError, ManagedHistory
from synai.models import ChatEvent, Message, ModelInfo, Session
from synai.sandbox import Sandbox
from synai.storage import ConversationStorage
from synai.tools import Tools


def complete_task(task: AgentTask) -> None:
    for state in (
        AgentStatus.UNDERSTANDING,
        AgentStatus.CONTEXT_GATHERING,
        AgentStatus.PLANNING,
        AgentStatus.IMPLEMENTING,
        AgentStatus.VERIFYING,
        AgentStatus.REVIEWING,
        AgentStatus.COMPLETED,
    ):
        task.transition(state)


class AgentStateTests(unittest.TestCase):
    def test_valid_state_transitions_include_repair_and_approval_resume(self) -> None:
        task = AgentTask("Implement a bounded retry")
        complete_task(task)
        self.assertEqual(task.status, AgentStatus.COMPLETED)

        repair = AgentTask("Implement a bounded retry")
        for state in (
            AgentStatus.UNDERSTANDING, AgentStatus.CONTEXT_GATHERING,
            AgentStatus.PLANNING, AgentStatus.IMPLEMENTING,
            AgentStatus.VERIFYING, AgentStatus.REPAIRING, AgentStatus.VERIFYING,
            AgentStatus.REVIEWING, AgentStatus.COMPLETED,
        ):
            repair.transition(state)
        self.assertEqual(repair.status, AgentStatus.COMPLETED)

        approval = AgentTask("Implement a bounded retry")
        for state in (
            AgentStatus.UNDERSTANDING, AgentStatus.CONTEXT_GATHERING,
            AgentStatus.PLANNING, AgentStatus.IMPLEMENTING,
        ):
            approval.transition(state)
        approval.transition(AgentStatus.WAITING_FOR_APPROVAL)
        approval.resolve_approval(True)
        self.assertEqual(approval.status, AgentStatus.IMPLEMENTING)
        self.assertIsNone(approval.approval_resume_state)

    def test_invalid_transitions_and_terminal_states_are_rejected(self) -> None:
        task = AgentTask("Task")
        with self.assertRaisesRegex(ValueError, "transition"):
            task.transition(AgentStatus.REVIEWING)
        with self.assertRaisesRegex(ValueError, "transition"):
            task.transition(AgentStatus.IDLE)

        for terminal in (AgentStatus.COMPLETED, AgentStatus.FAILED, AgentStatus.CANCELLED):
            with self.subTest(terminal=terminal):
                item = AgentTask("Task")
                if terminal == AgentStatus.COMPLETED:
                    complete_task(item)
                else:
                    item.transition(terminal)
                with self.assertRaisesRegex(ValueError, "terminal"):
                    item.transition(AgentStatus.UNDERSTANDING)

        denied = AgentTask("Task")
        denied.transition(AgentStatus.UNDERSTANDING)
        denied.transition(AgentStatus.CONTEXT_GATHERING)
        denied.transition(AgentStatus.PLANNING)
        denied.transition(AgentStatus.WAITING_FOR_APPROVAL)
        denied.resolve_approval(False)
        self.assertEqual(denied.status, AgentStatus.FAILED)
        with self.assertRaisesRegex(ValueError, "terminal"):
            denied.transition(AgentStatus.CONTEXT_GATHERING)

    def test_step_transitions_are_terminal_after_completion(self) -> None:
        step = AgentStep("step-1", "Implement retry", "Retry tests pass")
        step.transition(StepStatus.RUNNING)
        step.transition(StepStatus.COMPLETED)
        with self.assertRaisesRegex(ValueError, "transition"):
            step.transition(StepStatus.RUNNING)

    def test_typed_task_structures_roundtrip(self) -> None:
        step = AgentStep("step-1", "Implement retry", "Retry tests pass")
        step.transition(StepStatus.RUNNING)
        step.transition(StepStatus.COMPLETED)
        task = AgentTask("Implement a bounded retry", selected_model="local-model")
        task.plan = AgentPlan(task.goal, [step])
        task.current_step_id = step.step_id
        task.executions.append(AgentExecution(
            step_id=step.step_id, tool_name="patch_file", status=ExecutionStatus.SUCCEEDED,
            result_summary="Updated retry logic",
        ))
        task.verification_results.append(VerificationResult(
            command="pytest tests/test_retry.py", status=VerificationStatus.FAILED,
            exit_code=1, stderr="AssertionError", duration=0.5,
        ))
        task.repair_attempts.append(RepairAttempt(
            attempt=1, diagnosis="Retry limit is off by one", step_id=step.step_id,
            verification_index=0, status=RepairStatus.PENDING,
        ))
        for state in (
            AgentStatus.UNDERSTANDING, AgentStatus.CONTEXT_GATHERING,
            AgentStatus.PLANNING, AgentStatus.IMPLEMENTING,
            AgentStatus.VERIFYING, AgentStatus.REPAIRING,
        ):
            task.transition(state)

        restored = AgentTask.from_dict(task.to_dict())
        self.assertEqual(restored, task)
        checkpoint = AgentCheckpoint(task)
        restored_checkpoint = AgentCheckpoint.from_dict(json.loads(json.dumps(checkpoint.to_dict())))
        self.assertEqual(restored_checkpoint, checkpoint)

    def test_checkpoint_recovery_marks_uncertain_execution_and_task_interrupted(self) -> None:
        task = AgentTask("Apply a change")
        task.plan = AgentPlan(task.goal, [AgentStep("step-1", "Apply patch")])
        task.current_step_id = "step-1"
        for state in (
            AgentStatus.UNDERSTANDING, AgentStatus.CONTEXT_GATHERING,
            AgentStatus.PLANNING, AgentStatus.IMPLEMENTING,
        ):
            task.transition(state)
        task.executions.append(AgentExecution(step_id="step-1", tool_name="patch_file"))
        checkpoint = AgentCheckpoint(task)

        self.assertTrue(checkpoint.recover_interrupted())
        self.assertEqual(task.status, AgentStatus.INTERRUPTED)
        self.assertEqual(task.executions[0].status, ExecutionStatus.INTERRUPTED)
        self.assertIn("not replayed", task.terminal_summary)
        self.assertFalse(checkpoint.recover_interrupted())
        with self.assertRaisesRegex(ValueError, "terminal"):
            task.transition(AgentStatus.IMPLEMENTING)

    def test_pending_repair_mutation_is_interrupted_and_never_replayed(self) -> None:
        task = AgentTask("Repair a verified failure")
        step = AgentStep("step-1", "Repair the implementation")
        step.status = StepStatus.COMPLETED
        task.plan = AgentPlan(task.goal, [step])
        for state in (
            AgentStatus.UNDERSTANDING,
            AgentStatus.CONTEXT_GATHERING,
            AgentStatus.PLANNING,
            AgentStatus.IMPLEMENTING,
            AgentStatus.VERIFYING,
            AgentStatus.REPAIRING,
        ):
            task.transition(state)
        task.verification_results.append(VerificationResult(
            command="pytest tests/test_retry.py",
            status=VerificationStatus.FAILED,
            exit_code=1,
            stderr="AssertionError",
            duration=0.5,
        ))
        execution = AgentExecution(
            step_id=step.step_id,
            tool_name="patch_file",
            status=ExecutionStatus.PENDING,
        )
        task.executions.append(execution)
        task.repair_attempts.append(RepairAttempt(
            attempt=1,
            diagnosis="Repair action is pending.",
            step_id=step.step_id,
            verification_index=0,
            execution_ids=(execution.execution_id,),
            status=RepairStatus.PENDING,
        ))

        checkpoint = AgentCheckpoint(task)
        self.assertTrue(checkpoint.recover_interrupted())
        self.assertEqual(task.status, AgentStatus.INTERRUPTED)
        self.assertEqual(task.executions[0].status, ExecutionStatus.INTERRUPTED)
        self.assertEqual(task.repair_attempts[0].status, RepairStatus.INTERRUPTED)
        self.assertEqual(task.repair_outcome, RepairOutcome.REPAIR_INTERRUPTED)
        self.assertIn("not replayed", task.repair_attempts[0].error)
        self.assertFalse(checkpoint.recover_interrupted())

    def test_recovery_does_not_restart_verification_after_repair_mutation(self) -> None:
        task = AgentTask("Repair a verified failure")
        step = AgentStep("step-1", "Repair the implementation")
        step.status = StepStatus.COMPLETED
        task.plan = AgentPlan(task.goal, [step])
        task.repair_attempts.append(RepairAttempt(
            attempt=1,
            diagnosis="Applied a bounded repair.",
            step_id=step.step_id,
            status=RepairStatus.SUCCEEDED,
        ))
        task.repair_outcome = RepairOutcome.REPAIRED_PENDING_VERIFICATION
        for state in (
            AgentStatus.UNDERSTANDING,
            AgentStatus.CONTEXT_GATHERING,
            AgentStatus.PLANNING,
            AgentStatus.IMPLEMENTING,
            AgentStatus.VERIFYING,
        ):
            task.transition(state)

        checkpoint = AgentCheckpoint(task)
        self.assertTrue(checkpoint.recover_interrupted())
        self.assertEqual(task.status, AgentStatus.INTERRUPTED)
        self.assertEqual(task.repair_outcome, RepairOutcome.REPAIR_INTERRUPTED)
        self.assertEqual(task.repair_attempts[0].status, RepairStatus.SUCCEEDED)
        self.assertFalse(checkpoint.recover_interrupted())


class AgentCheckpointHistoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.settings = replace(Settings(), history_dir=self.root / ".synai")
        self.history = History(self.root / "history", self.settings)
        self.workspace = self.root / "project"
        self.workspace.mkdir()

    def test_optional_checkpoint_uses_schema_six_and_roundtrips(self) -> None:
        session = Session("model", self.settings.ollama_url, str(self.workspace))
        session.set_environment(ConversationEnvironment.from_settings(self.settings, self.workspace))
        self.history.save(session)
        legacy_data = json.loads(self.history.path_for(session.session_id).read_text())
        self.assertEqual(legacy_data["schema_version"], 5)
        self.assertNotIn("agent_checkpoint", legacy_data)

        task = AgentTask("Add a unit test")
        task.transition(AgentStatus.UNDERSTANDING)
        session.agent_checkpoint = AgentCheckpoint(task)
        self.history.save(session)
        data = json.loads(self.history.path_for(session.session_id).read_text())
        self.assertEqual(data["schema_version"], 6)
        self.assertIn("agent_checkpoint", data)

        loaded = self.history.load(self.history.path_for(session.session_id))
        self.assertEqual(loaded.schema_version, 6)
        self.assertEqual(loaded.agent_checkpoint, session.agent_checkpoint)

    def test_managed_history_accepts_agent_checkpoint_schema_six(self) -> None:
        storage = ConversationStorage(self.root / "managed")
        settings = replace(self.settings, history_dir=storage.root)
        history = ManagedHistory(storage, settings)
        session = Session("model", settings.ollama_url, "", schema_version=4)
        workspace = storage.workspace(session.session_id)
        storage.create(session.session_id, workspace=True)
        session.set_environment(ConversationEnvironment.from_settings(settings, workspace))
        session.managed_workspace_created = True
        session.agent_checkpoint = AgentCheckpoint(AgentTask("Inspect the workspace"))

        history.save(session)
        loaded = history.load(history.path_for(session.session_id))
        self.assertEqual(loaded.schema_version, 6)
        self.assertEqual(loaded.agent_checkpoint, session.agent_checkpoint)

    def test_schema_versions_one_through_five_remain_loadable(self) -> None:
        environment = asdict(ConversationEnvironment.from_settings(self.settings, self.workspace))
        base = Session("model", self.settings.ollama_url, str(self.workspace)).to_dict()
        base["limits"] = {
            "command_timeout": self.settings.command_timeout,
            "output_bytes": self.settings.output_bytes,
            "tool_budget": self.settings.tool_budget,
        }
        for version in range(1, 6):
            with self.subTest(version=version):
                data = dict(base, schema_version=version)
                data.pop("agent_checkpoint", None)
                if version == 1:
                    data["environment"] = None
                else:
                    saved_environment = dict(environment)
                    if version < 5:
                        saved_environment["ollama_url"] = self.settings.ollama_url
                        saved_environment["request_timeout"] = self.settings.request_timeout
                    if version < 3:
                        saved_environment.pop("execution_mode")
                    if version in {4, 5}:
                        data["managed_workspace_created"] = False
                    data["environment"] = saved_environment
                path = self.history.path_for(data["session_id"])
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(data))
                loaded = self.history.load(path)
                self.assertEqual(loaded.schema_version, version)
                self.assertIsNone(loaded.agent_checkpoint)

    def test_malformed_or_version_mismatched_checkpoint_is_rejected(self) -> None:
        session = Session("model", self.settings.ollama_url, str(self.workspace))
        session.set_environment(ConversationEnvironment.from_settings(self.settings, self.workspace))
        session.agent_checkpoint = AgentCheckpoint(AgentTask("Add a test"))
        self.history.save(session)
        path = self.history.path_for(session.session_id)
        original = json.loads(path.read_text())

        cases: list[tuple[str, object]] = []
        missing_task_id = json.loads(json.dumps(original))
        del missing_task_id["agent_checkpoint"]["task"]["task_id"]
        cases.append(("missing task field", missing_task_id))
        invalid_status = json.loads(json.dumps(original))
        invalid_status["agent_checkpoint"]["task"]["status"] = "untrusted"
        cases.append(("unknown status", invalid_status))
        extra_field = json.loads(json.dumps(original))
        extra_field["agent_checkpoint"]["task"]["unexpected"] = "value"
        cases.append(("unknown task field", extra_field))
        oversized_stdout = json.loads(json.dumps(original))
        oversized_stdout["agent_checkpoint"]["task"]["verification_results"] = [{
            "command": "pytest", "status": "passed", "exit_code": 0,
            "stdout": "x" * 65_537, "stderr": "", "duration": 1.0,
            "timed_out": False, "truncated": False,
        }]
        cases.append(("oversized output", oversized_stdout))

        for label, data in cases:
            with self.subTest(label=label):
                path.write_text(json.dumps(data))
                with self.assertRaises(HistoryError):
                    self.history.load(path)

        old_schema = dict(original, schema_version=5)
        path.write_text(json.dumps(old_schema))
        with self.assertRaisesRegex(HistoryError, "requires history version 6"):
            self.history.load(path)

    def test_invalid_checkpoint_is_rejected_on_save(self) -> None:
        session = Session("model", self.settings.ollama_url, str(self.workspace))
        session.set_environment(ConversationEnvironment.from_settings(self.settings, self.workspace))
        session.agent_checkpoint = AgentCheckpoint(AgentTask("Task"))
        session.agent_checkpoint.task.approval_resume_state = AgentStatus.IMPLEMENTING
        with self.assertRaisesRegex(HistoryError, "Invalid agent checkpoint"):
            self.history.save(session)

    def test_recovered_checkpoint_is_persisted_as_terminal_and_not_replayed(self) -> None:
        session = Session("model", self.settings.ollama_url, str(self.workspace))
        session.set_environment(ConversationEnvironment.from_settings(self.settings, self.workspace))
        task = AgentTask("Apply an uncertain patch")
        task.plan = AgentPlan(task.goal, [AgentStep("step-1", "Apply patch")])
        task.current_step_id = "step-1"
        for state in (
            AgentStatus.UNDERSTANDING, AgentStatus.CONTEXT_GATHERING,
            AgentStatus.PLANNING, AgentStatus.IMPLEMENTING,
        ):
            task.transition(state)
        task.executions.append(AgentExecution(step_id="step-1", tool_name="patch_file"))
        session.agent_checkpoint = AgentCheckpoint(task)
        self.history.save(session)

        loaded = self.history.load(self.history.path_for(session.session_id))
        Agent.recover(loaded)
        self.history.save(loaded)
        recovered = self.history.load(self.history.path_for(session.session_id))
        self.assertEqual(recovered.agent_checkpoint.task.status, AgentStatus.INTERRUPTED)
        self.assertEqual(
            recovered.agent_checkpoint.task.executions[0].status,
            ExecutionStatus.INTERRUPTED,
        )


class _TwoRoundProvider:
    def __init__(self) -> None:
        self.requests = []

    async def list_models(self) -> list[ModelInfo]:
        return []

    async def capabilities(self, name: str) -> ModelInfo:
        return ModelInfo(name, tools=True)

    async def close(self) -> None:
        return None

    async def chat(self, _model: str, messages: list, tools: list):
        del _model
        self.requests.append(messages)
        if not tools:
            yield ChatEvent(content="Finished.", done=True)
        elif len(self.requests) == 1:
            yield ChatEvent(
                tool_calls=[{"function": {"name": "read_file", "arguments": {"path": "source.py"}}}],
                done=True,
            )
        else:
            yield ChatEvent(content="Finished.", done=True)


class DynamicRequestContextTests(unittest.IsolatedAsyncioTestCase):
    async def test_environment_context_is_request_only_across_tool_rounds(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = replace(Settings(), history_dir=root / ".synai")
            sandbox = Sandbox(settings)
            sandbox.healthy = True
            sandbox.container_id = "test-container"
            session = Session(
                "model", settings.ollama_url, "", container_id=sandbox.container_id,
            )
            bind_sandbox(sandbox, session)
            sandbox.execute = AsyncMock(return_value={
                "ok": True, "content": "print('source')\n", "sha256": "content-hash",
            })

            provider = _TwoRoundProvider()
            history = History(root / "history", settings)
            agent = Agent(provider, history, Tools(sandbox, AsyncMock()), AsyncMock())
            await agent.turn(session, ModelInfo("model", tools=True), "Read the source")

            self.assertEqual(len(provider.requests), 2)
            dynamic = "Current execution environment:"
            for request in provider.requests:
                self.assertEqual(sum(dynamic in message.content for message in request), 1)
            self.assertEqual(sum(dynamic in message.content for message in session.messages), 0)
            self.assertEqual(sum(message.content == SYSTEM for message in session.messages), 1)

            loaded = history.load(history.path_for(session.session_id))
            self.assertEqual(sum(dynamic in message.content for message in loaded.messages), 0)
            self.assertEqual(sum(message.content == SYSTEM for message in loaded.messages), 1)
            sandbox.execute.assert_awaited_once()

    async def test_recovering_pending_chat_tool_does_not_execute_or_replay_it(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            settings = replace(Settings(), history_dir=Path(directory) / ".synai")
            sandbox = Sandbox(settings)
            sandbox.execute = AsyncMock()
            session = Session("model", settings.ollama_url, str(Path(directory)))

            session.state = "running"
            session.messages = [
                Message("user", "Run the command"),
                Message(
                    "assistant", tool_calls=[{
                        "function": {
                            "name": "terminal",
                            "arguments": {"command": "touch should-not-run", "cwd": "."},
                        },
                    }],
                    status="streaming",
                ),
            ]
            Agent.recover(session)
            self.assertEqual(session.state, "interrupted")
            self.assertEqual(session.messages[-1].role, "tool")
            self.assertIn("not replayed", session.messages[-1].content)
            sandbox.execute.assert_not_awaited()

    async def test_normal_chat_stays_a_single_ordinary_agent_turn(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            provider = _TwoRoundProvider()
            # No matching backend and a chat-only model mean no tool request.
            sandbox = Sandbox(replace(Settings(), history_dir=Path(directory) / ".synai"))
            history = History(Path(directory) / "history", sandbox.settings)
            agent = Agent(provider, history, Tools(sandbox, AsyncMock()), AsyncMock())
            session = Session("model", sandbox.settings.ollama_url, str(Path(directory)))
            await agent.turn(session, ModelInfo("model", tools=False), "Hello")

            self.assertEqual(len(provider.requests), 1)
            self.assertEqual(session.messages[-1].content, "Finished.")
            self.assertEqual(session.state, "idle")
            self.assertIsNone(session.agent_checkpoint)
            self.assertEqual(sum(message.content == SYSTEM for message in session.messages), 1)
