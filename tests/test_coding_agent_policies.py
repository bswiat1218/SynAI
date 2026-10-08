from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock

from synai.coding_agent.policies import (
    AutonomyMode,
    AutonomyPolicy,
    AutonomyPolicyConfig,
    OperationCategory,
    PolicyDecisionType,
    PolicyReason,
    PolicySource,
    classify_operation,
    inspect_policy,
)
from synai.coding_agent.state import AgentTask
from synai.config import Settings
from synai.models import Session
from synai.preferences import Preferences, PreferencesStore
from synai.tools import Tools


class PolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.policy = AutonomyPolicy()
        self.workspace = "/workspace"

    def request(
        self,
        tool: str,
        arguments: dict[str, object] | None = None,
        *,
        mode: AutonomyMode = AutonomyMode.AGENT,
        category: OperationCategory | None = None,
        backend: str = "sandbox",
        **constraints: bool,
    ):
        return self.policy.create_request(
            tool,
            arguments or {},
            mode=mode,
            task_id="task-1",
            step_id="step-1",
            backend_identity=backend,
            workspace=self.workspace,
            category=category,
            **constraints,
        )

    def test_modes_and_defaults_are_typed_and_agent_is_default(self) -> None:
        config = AutonomyPolicyConfig()
        config.validate()
        self.assertEqual(config.default_mode, AutonomyMode.AGENT)
        self.assertEqual(set(config.permitted_modes), set(AutonomyMode))
        for mode in AutonomyMode:
            mode_config = AutonomyPolicyConfig(
                default_mode=mode,
            )
            mode_config.validate()
            task_context = self.policy.create_task_context(
                mode, self.workspace, "sandbox",
            )
            self.assertEqual(task_context.mode, mode)
            self.assertEqual(len(task_context.policy_fingerprint), 64)

    def test_application_owned_operation_classification(self) -> None:
        cases = {
            "read_file": OperationCategory.FILE_READ,
            "list_files": OperationCategory.REPOSITORY_READ,
            "find_symbol": OperationCategory.REPOSITORY_INTELLIGENCE,
            "write_file": OperationCategory.FILE_MODIFICATION,
            "patch_file": OperationCategory.FILE_MODIFICATION,
            "delete_file": OperationCategory.FILE_DELETION,
            "terminal": OperationCategory.TERMINAL_EXECUTION,
            "fetch_url": OperationCategory.NETWORK_ACCESS,
            "git_status": OperationCategory.GIT_INSPECTION,
            "git_checkpoint": OperationCategory.GIT_CHECKPOINT_CREATION,
            "restore_checkpoint": OperationCategory.CHECKPOINT_RESTORATION,
            "safe_read_file": OperationCategory.UNKNOWN,
        }
        for name, category in cases.items():
            with self.subTest(name=name):
                self.assertEqual(classify_operation(name, {"category": "safe"}), category)
        self.assertEqual(
            classify_operation("write_file", {}, target_exists=False),
            OperationCategory.FILE_CREATION,
        )

    def test_sensitive_operations_retain_approval_in_every_mode(self) -> None:
        mandatory = {
            "write_file": PolicyReason.FILE_MUTATION_APPROVAL_REQUIRED,
            "patch_file": PolicyReason.FILE_MUTATION_APPROVAL_REQUIRED,
            "delete_file": PolicyReason.DELETION_APPROVAL_REQUIRED,
            "terminal": PolicyReason.TERMINAL_APPROVAL_REQUIRED,
            "fetch_url": PolicyReason.NETWORK_APPROVAL_REQUIRED,
            "git_status": PolicyReason.GIT_APPROVAL_REQUIRED,
            "git_diff": PolicyReason.GIT_APPROVAL_REQUIRED,
            "git_log": PolicyReason.GIT_APPROVAL_REQUIRED,
            "git_show": PolicyReason.GIT_APPROVAL_REQUIRED,
            "git_checkpoint": PolicyReason.CHECKPOINT_APPROVAL_REQUIRED,
            "restore_checkpoint": PolicyReason.CHECKPOINT_APPROVAL_REQUIRED,
        }
        for mode in AutonomyMode:
            for name, reason in mandatory.items():
                with self.subTest(mode=mode, tool=name):
                    decision = self.policy.evaluate(self.request(name, mode=mode))
                    self.assertEqual(decision.decision, PolicyDecisionType.REQUIRE_APPROVAL)
                    self.assertEqual(decision.reason, reason)

    def test_precedence_denies_hard_workspace_scope_cancellation_and_explicit_rules(self) -> None:
        base = self.request("read_file", {"path": "src/main.py"})
        hard = self.policy.evaluate(replace(base, hard_security_allowed=False))
        self.assertEqual(hard.reason, PolicyReason.HARD_SECURITY_RESTRICTION)
        workspace = self.policy.evaluate(replace(base, workspace_valid=False))
        self.assertEqual(workspace.reason, PolicyReason.WORKSPACE_MISMATCH)
        scope = self.policy.evaluate(replace(base, plan_scope_valid=False))
        self.assertEqual(scope.reason, PolicyReason.PLAN_SCOPE_VIOLATION)
        cancelled = self.policy.evaluate(base, cancelled=True)
        self.assertEqual(cancelled.reason, PolicyReason.CANCELLED)

        denied = AutonomyPolicy(AutonomyPolicyConfig(denied_tools=("read_file",)))
        decision = denied.evaluate(denied.create_request(
            "read_file", {"path": "src/main.py"},
            mode=AutonomyMode.AUTONOMOUS,
            task_id="task-1",
            step_id="step-1",
            backend_identity="sandbox",
            workspace=self.workspace,
        ))
        self.assertEqual(decision.decision, PolicyDecisionType.DENY)
        self.assertEqual(decision.reason, PolicyReason.EXPLICIT_TOOL_DENY)

    def test_unknown_and_invalid_operations_fail_closed(self) -> None:
        self.assertEqual(
            classify_operation("read_file_evil", {}),
            OperationCategory.UNKNOWN,
        )
        unknown = self.policy.create_request(
            "read_file_evil", {},
            mode=AutonomyMode.AGENT,
            task_id="task-1",
            step_id="step-1",
            backend_identity="sandbox",
            workspace=self.workspace,
        )
        self.assertEqual(
            self.policy.evaluate(unknown).reason,
            PolicyReason.UNKNOWN_OPERATION,
        )
        missing_workspace = self.policy.create_request(
            "read_file", {"path": "src/app.py"},
            mode=AutonomyMode.AGENT,
            task_id="task-1",
            step_id="step-1",
            backend_identity="sandbox",
            workspace="",
        )
        self.assertEqual(
            self.policy.evaluate(missing_workspace).reason,
            PolicyReason.WORKSPACE_MISMATCH,
        )
        malformed = AutonomyPolicyConfig(
            automatic_approval_exemptions=("terminal",),
        )
        with self.assertRaises(ValueError):
            malformed.validate()
        with self.assertRaises(ValueError):
            AutonomyPolicyConfig(denied_tools=("read_file", "read_file")).validate()
        with self.assertRaises(ValueError):
            AutonomyPolicyConfig(
                denied_tools=("search_code",),
                automatic_approval_exemptions=("search_code",),
            ).validate()
        config = AutonomyPolicyConfig()
        with self.assertRaises(ValueError):
            AutonomyPolicyConfig.from_dict(dict(
                config.to_dict(), schema_version=99,
            ))
        with self.assertRaises(ValueError):
            AutonomyPolicyConfig.from_dict(dict(
                config.to_dict(), automatic_approval_exemptions=["*"],
            ))

    def test_explicit_read_exemption_is_exact_sandbox_only_and_auditable(self) -> None:
        policy = AutonomyPolicy(AutonomyPolicyConfig(
            automatic_approval_exemptions=("search_code",),
        ))
        sandbox = policy.evaluate(policy.create_request(
            "search_code", {"query": "needle"},
            mode=AutonomyMode.AUTONOMOUS,
            task_id="task-1",
            step_id="step-1",
            backend_identity="sandbox",
            workspace=self.workspace,
        ))
        self.assertEqual(sandbox.decision, PolicyDecisionType.ALLOW)
        self.assertEqual(sandbox.reason, PolicyReason.EXPLICIT_ELIGIBLE_EXEMPTION)
        self.assertTrue(sandbox.explicit_configuration)
        self.assertIn("Policy source:", inspect_policy(sandbox))

        host = policy.evaluate(policy.create_request(
            "search_code", {"query": "needle"},
            mode=AutonomyMode.AUTONOMOUS,
            task_id="task-1",
            step_id="step-1",
            backend_identity="host",
            workspace=self.workspace,
        ))
        self.assertEqual(host.decision, PolicyDecisionType.ALLOW)
        self.assertEqual(host.reason, PolicyReason.SAFE_READ_ALLOWED)
        self.assertFalse(host.explicit_configuration)
        self.assertEqual(host.source, PolicySource.BUILT_IN_DEFAULT)

    def test_policy_change_invalidates_a_previous_task_request(self) -> None:
        request = self.request("find_symbol", {"name": "Widget"})
        self.policy.replace_configuration(AutonomyPolicyConfig(
            denied_tools=("find_symbol",),
        ))
        decision = self.policy.evaluate(request)
        self.assertEqual(decision.decision, PolicyDecisionType.DENY)
        self.assertEqual(decision.reason, PolicyReason.POLICY_CHANGED)

    def test_task_policy_context_and_audit_round_trip_additively(self) -> None:
        task = AgentTask("inspect source")
        task.policy_context = self.policy.create_task_context(
            AutonomyMode.AGENT, self.workspace, "sandbox",
        )
        request = self.request("read_file", {"path": "src/main.py"})
        decision = self.policy.evaluate(request)
        from synai.coding_agent.policies import PolicyAuditRecord

        task.policy_audit.append(PolicyAuditRecord(
            task_id=task.task_id,
            step_id="step-1",
            execution_id="execution-1",
            tool_name=decision.tool_name,
            category=decision.category,
            mode=decision.mode,
            decision=decision.decision,
            reason=decision.reason,
            policy_fingerprint=decision.policy_fingerprint,
            approval_required=False,
            approval_outcome="not_required",
            backend_identity="sandbox",
            workspace_identity=task.policy_context.workspace_identity,
            timestamp="2026-10-08T00:00:00+00:00",
        ))
        restored = AgentTask.from_dict(task.to_dict())
        self.assertEqual(restored.policy_context, task.policy_context)
        self.assertEqual(restored.policy_audit, task.policy_audit)

    def test_preferences_read_legacy_settings_and_persist_typed_policy(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = PreferencesStore(Path(directory))
            legacy = {
                "schema_version": 1,
                "ollama_url": "http://localhost:11434",
                "request_timeout": 1200,
                "theme": "synai-cyberpunk",
            }
            store.save(Preferences())
            store.path.write_text(json.dumps(legacy), encoding="utf-8")
            self.assertEqual(store.load().autonomy_policy, AutonomyPolicyConfig())

            configured = Preferences(autonomy_policy=AutonomyPolicyConfig(
                automatic_approval_exemptions=("search_code",),
            ))
            store.save(configured)
            self.assertEqual(store.load(), configured)


class DispatcherPolicyTests(unittest.IsolatedAsyncioTestCase):
    async def test_changed_arguments_and_malformed_approval_never_dispatch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)

            class Backend:
                def __init__(self) -> None:
                    self.settings = Settings(
                        execution_mode="host", history_dir=workspace / ".synai",
                    )
                    self.workspace = workspace
                    self.execution_workspace = workspace
                    self.calls: list[tuple[str, dict[str, object]]] = []

                def matches(self, session: Session) -> bool:
                    del session
                    return True

                async def execute(self, name, arguments, expected_sha256=None):
                    del expected_sha256
                    self.calls.append((name, arguments))
                    return {"ok": True}

            backend = Backend()
            approval = AsyncMock(return_value=True)
            tools = Tools(backend, approval)
            session = Session("model", "http://localhost", str(workspace))
            policy = tools.policy
            request = policy.create_request(
                "read_file",
                {"path": "old.txt"},
                mode=AutonomyMode.AGENT,
                task_id="task-1",
                step_id="step-1",
                backend_identity="host",
                workspace=str(workspace),
            )
            result = await tools.call(
                "read_file", {"path": "new.txt"},
                session=session, policy_request=request,
            )
            self.assertTrue(result["denied"])
            self.assertEqual(result["error_code"], "POLICY_DENIED")
            self.assertEqual(backend.calls, [])

            malformed = Tools(backend, AsyncMock(return_value=1))
            result = await malformed.call(
                "terminal", {"command": "echo safe", "cwd": "."},
            )
            self.assertTrue(result["denied"])
            self.assertEqual(result["error"], "User denied this action")
            self.assertEqual(backend.calls, [])

    async def test_policy_revocation_during_approval_prevents_dispatch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)

            class Backend:
                def __init__(self) -> None:
                    self.settings = Settings(
                        execution_mode="host", history_dir=workspace / ".synai",
                    )
                    self.workspace = workspace
                    self.execution_workspace = workspace
                    self.calls: list[tuple[str, dict[str, object]]] = []

                def matches(self, session: Session) -> bool:
                    del session
                    return True

                async def execute(self, name, arguments, expected_sha256=None):
                    del expected_sha256
                    self.calls.append((name, arguments))
                    return {"ok": True}

            backend = Backend()
            tools: Tools

            async def revoke(_name: str, _description: str) -> bool:
                tools.policy.replace_configuration(AutonomyPolicyConfig(
                    denied_tools=("terminal",),
                ))
                return True

            tools = Tools(backend, revoke)
            session = Session("model", "http://localhost", str(workspace))
            request = tools.policy.create_request(
                "terminal",
                {"command": "echo safe", "cwd": "."},
                mode=AutonomyMode.AUTONOMOUS,
                task_id="task-1",
                step_id="step-1",
                backend_identity="host",
                workspace=str(workspace),
            )
            result = await tools.call(
                "terminal",
                {"command": "echo safe", "cwd": "."},
                session=session,
                policy_request=request,
            )
            self.assertTrue(result["denied"])
            self.assertEqual(result["error_code"], "POLICY_DENIED")
            self.assertEqual(result["policy_reason"], PolicyReason.POLICY_CHANGED.value)
            self.assertEqual(backend.calls, [])

    async def test_scope_denial_occurs_before_existing_approval_or_backend(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)

            class Backend:
                def __init__(self) -> None:
                    self.settings = Settings(
                        execution_mode="host", history_dir=workspace / ".synai",
                    )
                    self.workspace = workspace
                    self.execution_workspace = workspace
                    self.calls: list[tuple[str, dict[str, object]]] = []

                def matches(self, session: Session) -> bool:
                    del session
                    return True

                async def execute(self, name, arguments, expected_sha256=None):
                    del expected_sha256
                    self.calls.append((name, arguments))
                    return {"ok": True, "content": "source"}

            backend = Backend()
            approval = AsyncMock(return_value=True)
            tools = Tools(backend, approval)
            session = Session("model", "http://localhost", str(workspace))
            request = tools.policy.create_request(
                "patch_file",
                {"path": "src.py", "old": "a", "new": "b"},
                mode=AutonomyMode.AUTONOMOUS,
                task_id="task-1",
                step_id="step-1",
                backend_identity="host",
                workspace=str(workspace),
                category=OperationCategory.FILE_MODIFICATION,
                plan_scope_valid=False,
            )
            result = await tools.call(
                "patch_file",
                {"path": "src.py", "old": "a", "new": "b"},
                session=session,
                policy_request=request,
            )
            self.assertTrue(result["denied"])
            self.assertEqual(result["policy_reason"], PolicyReason.PLAN_SCOPE_VIOLATION.value)
            approval.assert_not_awaited()
            self.assertEqual(backend.calls, [])
