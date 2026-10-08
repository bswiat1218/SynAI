from __future__ import annotations

import asyncio
import json
import hashlib
import logging
import math
import os
import re
import shlex
import stat
import threading
import time
import tomllib
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from synai.coding_agent.context import ContextKind, ContextPackage
from synai.coding_agent.state import (
    AgentCheckpoint,
    AgentPlan,
    AgentStatus,
    AgentTask,
    ApprovalStatus,
    ExecutionStatus,
    PlanOperation,
    Repairability,
    StepStatus,
    VerificationCheck,
    VerificationIntent,
    VerificationOutcome,
    VerificationPlan,
    VerificationResult,
    VerificationStatus,
)
from synai.coding_agent.policies import (
    AutonomyMode,
    OperationCategory,
    PolicyAuditRecord,
    PolicyDecisionType,
    workspace_fingerprint,
)

from synai.execution_backend import validate_workspace
from synai.intelligence import RepositoryIndex
from synai.models import Session
from synai.tools import Tools


_INTENT_ORDER = {
    VerificationIntent.SYNTAX_CHECK: 0,
    VerificationIntent.TARGETED_TESTS: 1,
    VerificationIntent.RELEVANT_TESTS: 2,
    VerificationIntent.LINT: 3,
    VerificationIntent.TYPE_CHECK: 4,
    VerificationIntent.BUILD: 5,
    VerificationIntent.FULL_TEST_SUITE: 6,
}
_EXCLUDED_DIRS = {
    ".git", ".hg", ".svn", ".venv", "venv", "env", "node_modules",
    "__pycache__", ".mypy_cache", ".pytest_cache", ".ruff_cache",
    "dist", "build", "target", ".tox",
}
_MUTATIONS = {
    PlanOperation.CREATE, PlanOperation.MODIFY, PlanOperation.DELETE, PlanOperation.DOCUMENT,
}
_FAILURE_HINT = re.compile(
    r"(?:FAILED\b|ERROR\b|FAIL:|AssertionError|SyntaxError|TypeError|"
    r"error\[E\d{4}\]|error TS\d+|error:|Traceback \(most recent call last\))",
    re.IGNORECASE,
)
_logger = logging.getLogger(__name__)


def _verification_requirements_fingerprint(plan: VerificationPlan) -> str:
    serialized = json.dumps(
        [check.to_dict() for check in plan.checks],
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class VerificationLimits:
    max_checks: int = 16
    max_runs: int = 4
    fail_fast_on_syntax_failure: bool = True
    max_command_seconds: float = 300
    max_total_seconds: float = 900
    max_stdout_bytes: int = 24_000
    max_stderr_bytes: int = 24_000
    max_check_output_bytes: int = 48_000
    max_total_output_bytes: int = 384_000
    max_diagnostics: int = 24
    max_targeted_paths: int = 8
    max_relevant_paths: int = 8
    max_discovery_entries: int = 12_000
    max_discovery_seconds: float = 3

    def __post_init__(self) -> None:
        integers = (
            self.max_checks, self.max_runs, self.max_stdout_bytes, self.max_stderr_bytes,
            self.max_check_output_bytes, self.max_total_output_bytes,
            self.max_diagnostics, self.max_targeted_paths, self.max_relevant_paths,
            self.max_discovery_entries,
        )
        if any(type(value) is not int or value < 1 for value in integers):
            raise ValueError("Verification limits must be positive integers")
        if (
            self.max_checks > 64 or self.max_runs > 8 or self.max_stdout_bytes > 65_536
            or self.max_stderr_bytes > 65_536 or self.max_check_output_bytes > 131_072
            or self.max_total_output_bytes > 1_048_576 or self.max_diagnostics > 128
            or self.max_targeted_paths > 32 or self.max_relevant_paths > 32
            or self.max_discovery_entries > 100_000
        ):
            raise ValueError("Verification limits exceed hard safety bounds")
        if type(self.fail_fast_on_syntax_failure) is not bool:
            raise ValueError("Verification fail-fast policy must be a boolean")
        for value in (
            self.max_command_seconds, self.max_total_seconds, self.max_discovery_seconds,
        ):
            if (
                isinstance(value, bool) or not isinstance(value, (int, float))
                or not math.isfinite(value) or value <= 0 or value > 86_400
            ):
                raise ValueError("Verification time limits must be finite and bounded")


@dataclass(frozen=True)
class VerificationRequest:
    task: AgentTask
    session: Session
    repository: RepositoryIndex
    context: ContextPackage | None = None
    cancellation: threading.Event | None = None
    provider_name: str | None = None


@dataclass(frozen=True)
class VerificationRunResult:
    task_id: str
    state: AgentStatus
    outcome: VerificationOutcome
    plan: VerificationPlan | None
    results: tuple[VerificationResult, ...]
    ready_for_review: bool
    repair_required: bool
    no_verification_needed: bool
    error: str | None = None


@dataclass(frozen=True)
class _Project:
    root: str
    kind: str
    manifest: dict[str, Any]
    evidence: frozenset[str]
    test_paths: tuple[str, ...]
    unittest_style: bool
    pytest_configured: bool


Checkpoint = Any
EventSink = Any


class VerificationEngine:
    """Deterministic project-aware verification through SynAI's registered terminal tool."""

    def __init__(
        self,
        tools: Tools,
        *,
        limits: VerificationLimits | None = None,
        checkpoint: Checkpoint | None = None,
        event_sink: EventSink | None = None,
    ) -> None:
        self.tools = tools
        self.limits = limits or VerificationLimits()
        self.checkpoint = checkpoint
        self.event_sink = event_sink

    async def verify(self, request: VerificationRequest) -> VerificationRunResult:
        task = request.task
        started = time.monotonic()
        had_verification_state = (
            isinstance(task, AgentTask)
            and (task.verification_plan is not None or task.verification_outcome is not None)
        )
        if not isinstance(request, VerificationRequest):
            return self._result(task, VerificationOutcome.ERROR, error="A typed verification request is required")
        if not isinstance(task, AgentTask) or not isinstance(request.session, Session):
            return self._result(task, VerificationOutcome.ERROR, error="Task and session must be typed values")
        try:
            self._validate_request(request)
            if request.cancellation and request.cancellation.is_set():
                return await self._cancel_before_start(task)
            if task.verification_plan is not None or task.verification_outcome is not None:
                raise ValueError("This task already has a verification run; verification is never replayed")

            intents = self._intents(task.plan)
            if not intents:
                if not _is_read_only_without_verification(task):
                    raise ValueError(
                        "A task with mutation, test, or verification operations requires verification intent",
                    )
                task.verification_outcome = VerificationOutcome.NO_VERIFICATION_NEEDED
                task.terminal_summary = "No executable verification intent was required."
                await self._checkpoint(task)
                await self._emit(task, "verification_not_required")
                return self._result(task, VerificationOutcome.NO_VERIFICATION_NEEDED, no_verification_needed=True)

            task.verification_outcome = VerificationOutcome.UNKNOWN
            await self._checkpoint(task)
            await self._emit(task, "verification_started")
            plan = self._build_plan(request, intents)
            plan.requirements_fingerprint = _verification_requirements_fingerprint(plan)
            task.verification_plan = plan
            await self._checkpoint(task)
            await self._emit(task, "verification_plan_ready", message=self._render_plan(plan))

            calls = 0
            total_task_calls = len(task.executions)
            total_output = sum(
                len(item.stdout.encode("utf-8")) + len(item.stderr.encode("utf-8"))
                for item in task.verification_results
            )
            syntax_failed = False
            for check in plan.checks:
                if request.cancellation and request.cancellation.is_set():
                    return await self._cancel_run(task, check.check_id)
                if time.monotonic() - started >= self.limits.max_total_seconds:
                    self._append_nonrun_result(
                        task, check, VerificationStatus.BLOCKED,
                        "Total verification time limit exhausted.",
                        repairability=Repairability.NOT_REPAIRABLE,
                    )
                    await self._checkpoint(task)
                    continue
                dependent_on_syntax = check.intent in {
                    VerificationIntent.TARGETED_TESTS,
                    VerificationIntent.RELEVANT_TESTS,
                    VerificationIntent.TYPE_CHECK,
                    VerificationIntent.BUILD,
                    VerificationIntent.FULL_TEST_SUITE,
                }
                if (
                    syntax_failed
                    and self.limits.fail_fast_on_syntax_failure
                    and dependent_on_syntax
                ):
                    self._append_nonrun_result(
                        task, check, VerificationStatus.SKIPPED,
                        "Skipped after a syntax check failed.",
                        repairability=Repairability.NOT_REPAIRABLE,
                    )
                    await self._checkpoint(task)
                    continue
                if not check.available:
                    self._append_nonrun_result(
                        task, check, VerificationStatus.UNAVAILABLE, check.discovery_reason,
                        repairability=Repairability.NOT_REPAIRABLE,
                    )
                    await self._checkpoint(task)
                    await self._emit(task, "check_blocked", check_id=check.check_id, message=check.discovery_reason)
                    continue
                if (
                    calls >= self.limits.max_checks
                    or total_task_calls >= self.tools.sandbox.settings.tool_budget
                ):
                    self._append_nonrun_result(
                        task, check, VerificationStatus.BLOCKED,
                        "Verification tool-call budget exhausted.",
                        repairability=Repairability.NOT_REPAIRABLE,
                    )
                    await self._checkpoint(task)
                    continue
                try:
                    root = self._validate_request(request)
                    self._validate_check(check, root)
                    current_plan = self._build_plan(request, intents)
                    current_check = next(
                        (item for item in current_plan.checks if item.check_id == check.check_id),
                        None,
                    )
                    if current_check is None or current_check.to_dict() != check.to_dict():
                        raise ValueError("Verification check no longer matches current project evidence")
                except (OSError, ValueError) as exc:
                    self._append_nonrun_result(
                        task, check, VerificationStatus.BLOCKED, str(exc)[:2048],
                        repairability=Repairability.NOT_REPAIRABLE,
                    )
                    await self._checkpoint(task)
                    await self._emit(task, "check_blocked", check_id=check.check_id, message=str(exc)[:1024])
                    continue

                result = VerificationResult(
                    command=shlex.join(check.argv),
                    status=VerificationStatus.RUNNING,
                    check_id=check.check_id,
                    intent=check.intent,
                    verifier=check.project_type,
                    cwd=check.cwd,
                    required=check.required,
                    approval_state=ApprovalStatus.PENDING,
                    relevant_paths=check.relevant_paths,
                    repairability=Repairability.UNKNOWN,
                    run_id=plan.run_id,
                )
                task.verification_results.append(result)
                task.current_verification_check_id = check.check_id
                await self._checkpoint(task)
                await self._emit(task, "check_started", check_id=check.check_id)
                calls += 1
                total_task_calls += 1
                remaining = max(0.01, self.limits.max_total_seconds - (time.monotonic() - started))
                timeout = min(check.timeout_seconds, self.tools.sandbox.settings.command_timeout, remaining)
                try:
                    tool_result = await self._run_terminal(request, result, check, timeout)
                except asyncio.CancelledError:
                    result.status = VerificationStatus.INTERRUPTED
                    result.infrastructure_error = "Terminal result is uncertain; this command will not be replayed."
                    result.repairability = Repairability.NOT_REPAIRABLE
                    result.timed_out = False
                    task.current_verification_check_id = None
                    await self._checkpoint(task)
                    return await self._cancel_run(task, check.check_id, existing=result)
                except TimeoutError:
                    tool_result = {
                        "ok": False, "timed_out": True,
                        "error": "Verification command exceeded its execution deadline",
                    }
                except Exception as exc:
                    if task.status == AgentStatus.WAITING_FOR_APPROVAL:
                        task.status = AgentStatus.VERIFYING
                        task.approval_resume_state = None
                    result.status = VerificationStatus.ERROR
                    result.infrastructure_error = f"Verification orchestration failed: {str(exc)[:1024]}"
                    result.repairability = Repairability.NOT_REPAIRABLE
                    task.current_verification_check_id = None
                    await self._checkpoint(task)
                    await self._emit(task, "check_blocked", check_id=check.check_id, message=result.infrastructure_error)
                    continue

                self._apply_tool_result(
                    result, tool_result, check,
                    output_remaining=max(0, self.limits.max_total_output_bytes - total_output),
                )
                total_output += len(result.stdout.encode("utf-8")) + len(result.stderr.encode("utf-8"))
                task.current_verification_check_id = None
                await self._checkpoint(task)
                await self._emit(
                    task, self._event_for_status(result.status),
                    check_id=check.check_id,
                    message=result.failure_summary or result.infrastructure_error,
                )
                if result.status == VerificationStatus.FAILED and check.intent == VerificationIntent.SYNTAX_CHECK:
                    syntax_failed = True

            outcome = self._overall(plan, task.verification_results)
            if plan.requirements_fingerprint != _verification_requirements_fingerprint(plan):
                outcome = VerificationOutcome.BLOCKED
            if outcome == VerificationOutcome.PASSED:
                plan.source_fingerprints = self._capture_source_fingerprints(request)
            task.verification_outcome = outcome
            if outcome == VerificationOutcome.PASSED:
                task.transition(AgentStatus.REVIEWING)
                task.terminal_summary = "Verification passed; ready for review."
                await self._checkpoint(task)
                await self._emit(task, "ready_for_review")
            elif outcome == VerificationOutcome.CODE_FAILURE:
                task.transition(AgentStatus.REPAIRING)
                task.terminal_summary = "Verification found project/code failures; repair is required."
                await self._checkpoint(task)
                await self._emit(task, "repair_required")
            elif outcome == VerificationOutcome.CANCELLED:
                task.transition(AgentStatus.CANCELLED)
                task.terminal_summary = "Verification cancelled; uncertain commands will not be replayed."
                await self._checkpoint(task)
                await self._emit(task, "verification_cancelled")
            else:
                task.terminal_summary = (
                    "Verification could not establish correctness because of a policy or infrastructure block."
                    if outcome == VerificationOutcome.BLOCKED
                    else "Verification encountered an operational error."
                )
                await self._checkpoint(task)
                await self._emit(
                    task,
                    "verification_blocked" if outcome == VerificationOutcome.BLOCKED else "verification_error",
                )
            return self._result(
                task, outcome,
                ready_for_review=outcome == VerificationOutcome.PASSED,
                repair_required=outcome == VerificationOutcome.CODE_FAILURE,
            )
        except asyncio.CancelledError:
            return await self._cancel_run(task, task.current_verification_check_id)
        except InterruptedError as exc:
            if request.cancellation and request.cancellation.is_set():
                return await self._cancel_run(task, task.current_verification_check_id)
            task.verification_outcome = VerificationOutcome.ERROR
            await self._checkpoint(task)
            return self._result(task, VerificationOutcome.ERROR, error=str(exc)[:2048])
        except (OSError, TypeError, ValueError) as exc:
            return await self._record_error(
                task, str(exc), preserve=had_verification_state,
            )
        except Exception as exc:
            return await self._record_error(
                task,
                f"Unexpected verification subsystem failure: {str(exc)[:1024]}",
                preserve=had_verification_state,
            )

    async def restart_interrupted(
        self,
        request: VerificationRequest,
    ) -> VerificationRunResult:
        """Explicitly restart an interrupted run; uncertain checks are never replayed."""
        task = request.task
        try:
            if not isinstance(task, AgentTask) or task.status != AgentStatus.INTERRUPTED:
                raise ValueError("Only an interrupted Agent Task can restart verification")
            if task.verification_outcome not in {None, VerificationOutcome.UNKNOWN}:
                raise ValueError("Only an incomplete verification run can be restarted")
            self._validate_request(request, allow_interrupted=True)
            previous_runs = {
                item.run_id for item in task.verification_results if item.run_id is not None
            }
            if task.verification_plan is not None:
                previous_runs.add(task.verification_plan.run_id)
            if len(previous_runs) >= self.limits.max_runs:
                raise ValueError("Verification restart limit exhausted")
            if any(item.status == ExecutionStatus.PENDING for item in task.executions):
                raise ValueError("Verification cannot restart with uncertain implementation calls")
            previous_state = (
                task.status,
                task.verification_plan,
                task.verification_outcome,
                task.current_verification_check_id,
                task.updated_at,
            )
            task.status = AgentStatus.VERIFYING
            task.verification_plan = None
            task.verification_outcome = None
            task.current_verification_check_id = None
            task.updated_at = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime())
            try:
                await self._checkpoint(task)
                await self._emit(task, "verification_restarted")
            except BaseException:
                (
                    task.status,
                    task.verification_plan,
                    task.verification_outcome,
                    task.current_verification_check_id,
                    task.updated_at,
                ) = previous_state
                raise
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return self._result(task, VerificationOutcome.ERROR, error=str(exc)[:2048])
        return await self.verify(request)

    def build_plan(self, request: VerificationRequest) -> VerificationPlan:
        """Build an application-owned plan without executing a provider or terminal command."""
        self._validate_request(request)
        return self._build_plan(request, self._intents(request.task.plan))

    def _validate_request(
        self,
        request: VerificationRequest,
        *,
        allow_interrupted: bool = False,
    ) -> Path:
        task, session, repository = request.task, request.session, request.repository
        task.validate()
        allowed_states = {AgentStatus.VERIFYING}
        if allow_interrupted:
            allowed_states.add(AgentStatus.INTERRUPTED)
        if task.status not in allowed_states or task.plan is None:
            raise ValueError("Verification requires a task in VERIFYING with a validated plan")
        task.plan.validate()
        if task.plan.planner_model is not None and task.selected_model != task.plan.planner_model:
            raise ValueError("Selected model differs from the validated plan provenance")
        if not task.plan.planner_model or not task.plan.planner_provider:
            raise ValueError("Verification requires provider/model provenance from a validated plan")
        if request.provider_name is not None and (
            not isinstance(request.provider_name, str)
            or request.provider_name[:128] != task.plan.planner_provider
        ):
            raise ValueError("Active provider differs from the provider that produced the validated plan")
        if not isinstance(repository, RepositoryIndex):
            raise ValueError("Verification requires the active repository index")
        backend = self.tools.sandbox
        if not backend.matches(session):
            raise ValueError("Execution backend does not match the active conversation workspace")
        if session.environment is None or session.environment.workspace != str(Path(session.workspace).resolve()):
            raise ValueError("Conversation workspace identity changed")
        root = validate_workspace(
            Path(session.workspace), backend.settings,
            sandbox=backend.settings.execution_mode == "sandbox",
        )
        if root != repository.root or backend.workspace is None or backend.workspace.resolve() != root:
            raise ValueError("Repository index or execution backend workspace changed")
        if request.cancellation is not None and not isinstance(request.cancellation, threading.Event):
            raise ValueError("Cancellation must be a threading.Event")
        if request.context is not None:
            if not isinstance(request.context, ContextPackage) or request.context.task != task.goal:
                raise ValueError("Verification context does not match the Agent Task")
            ContextPackage.from_dict(request.context.to_dict())
        if any(step.status != StepStatus.COMPLETED for step in task.plan.steps):
            raise ValueError("Verification cannot start before all planned implementation steps complete")
        steps = {step.step_id: step for step in task.plan.steps}
        for execution in task.executions:
            if (
                execution.status == ExecutionStatus.SUCCEEDED
                and execution.operation in _MUTATIONS and execution.target_path is not None
            ):
                step = steps.get(execution.step_id)
                if (
                    step is None or execution.operation not in step.operations
                    or execution.target_path not in step.paths
                ):
                    raise ValueError("Successful mutation record is outside the validated plan scope")
                _validate_relative(root, execution.target_path, allow_missing=True)
        return root

    @staticmethod
    def _capture_source_fingerprints(
        request: VerificationRequest,
    ) -> dict[str, str]:
        if request.task.plan is None:
            return {}
        plan_paths = (
            path
            for step in request.task.plan.steps
            if set(step.operations) & _MUTATIONS
            for path in step.paths
        )
        execution_paths = (
            execution.target_path
            for execution in request.task.executions
            if execution.status == ExecutionStatus.SUCCEEDED
            and execution.operation in _MUTATIONS
            and execution.target_path is not None
        )
        context_paths = (
            item.path
            for item in request.context.items
            if item.path is not None
        ) if request.context is not None else ()
        paths = tuple(dict.fromkeys((
            *plan_paths, *execution_paths, *context_paths,
        )))
        if not paths:
            return {}
        snapshots = request.repository.read_sources(paths[:128], request.cancellation)
        fingerprints: dict[str, str] = {}
        for path in paths[:128]:
            if request.cancellation and request.cancellation.is_set():
                raise InterruptedError("Verification fingerprint capture cancelled")
            try:
                target = _validate_relative(
                    request.repository.root, path, allow_missing=True,
                )
                if not target.exists():
                    fingerprints[path] = "missing"
                    continue
                snapshot = snapshots[path]
                if snapshot is not None:
                    fingerprints[path] = snapshot.sha256
            except (OSError, ValueError):
                continue
        return fingerprints

    def _build_plan(
        self,
        request: VerificationRequest,
        intents: tuple[VerificationIntent, ...],
    ) -> VerificationPlan:
        root = Path(request.session.workspace).resolve()
        projects, all_tests, scan_warnings = _discover_projects(
            root, self.limits, request.cancellation,
        )
        changed = tuple(dict.fromkeys(
            execution.target_path
            for execution in request.task.executions
            if execution.status == ExecutionStatus.SUCCEEDED
            and execution.operation in _MUTATIONS
            and execution.target_path is not None
        ))
        declared = tuple(
            path for step_id in request.task.plan.executable_order
            for step in request.task.plan.steps if step.step_id == step_id
            for path in step.paths
        )
        scope_paths = tuple(dict.fromkeys((*changed, *declared)))
        selected = _projects_for_paths(projects, scope_paths)
        if not selected:
            if any(path.endswith(".py") for path in scope_paths):
                selected = [_fallback_python(root, all_tests)]
            else:
                selected = [_Project(".", "unknown", {}, frozenset(), all_tests, False, False)]
        selected.sort(key=lambda project: project.root)
        checks: list[VerificationCheck] = []
        warnings = list(scan_warnings)
        omitted_projects = len(selected) > 16
        if omitted_projects:
            selected = selected[:16]
            warnings.append("Project-root limit reached; remaining project roots are not verified.")
        if len(selected) > 1:
            warnings.append("Changed plan paths span multiple project roots; checks are scoped per root.")
        test_targets = _test_targets(
            selected, scope_paths, request, self.limits,
        )
        declared_tests = {path for path in scope_paths if _looks_like_test(path)}
        for intent in sorted(intents, key=lambda value: _INTENT_ORDER[value]):
            for project in selected:
                targets = test_targets.get(project.root, ())
                if intent == VerificationIntent.TARGETED_TESTS:
                    targets = tuple(path for path in targets if path in declared_tests)
                    targets = targets[:self.limits.max_targeted_paths]
                elif intent == VerificationIntent.RELEVANT_TESTS:
                    targets = targets[:self.limits.max_relevant_paths]
                created = _checks_for_intent(
                    intent, project, root, scope_paths,
                    targets, self.limits,
                )
                checks.extend(created)
        checks = [
            replace(
                check,
                timeout_seconds=min(
                    check.timeout_seconds,
                    self.tools.sandbox.settings.command_timeout,
                ),
            )
            for check in checks
        ]
        omitted_checks = len(checks) > self.limits.max_checks
        if omitted_checks:
            checks = checks[:self.limits.max_checks]
            warnings.append("Verification check limit reached; remaining requested checks were not planned.")
        return VerificationPlan(
            project_type=(
                selected[0].kind if len({item.kind for item in selected}) == 1 else "multi_project"
            ),
            project_roots=tuple(project.root for project in selected),
            checks=checks,
            unsupported_intents=[
                intent for intent in intents
                if omitted_projects or omitted_checks
                or not any(check.intent == intent and check.available for check in checks)
            ],
            warnings=warnings[:64],
        )

    def _intents(self, plan: AgentPlan | None) -> tuple[VerificationIntent, ...]:
        if plan is None:
            return ()
        return tuple(sorted(
            set(plan.verification_intent).union(
                intent for step in plan.steps for intent in step.verification_intents
            ),
            key=lambda value: _INTENT_ORDER[value],
        ))

    def _validate_check(self, check: VerificationCheck, root: Path) -> None:
        check.validate()
        if not check.available or not check.argv:
            raise ValueError("Unavailable verification checks cannot be dispatched")
        cwd = _validate_relative(root, check.cwd, allow_root=True)
        if not cwd.is_dir():
            raise ValueError("Verification working directory is no longer available")
        for path in check.relevant_paths:
            _validate_relative(root, path, allow_missing=False)
        if check.timeout_seconds > min(
            self.limits.max_command_seconds, self.tools.sandbox.settings.command_timeout,
        ):
            raise ValueError("Verification check exceeds current timeout policy")
        if check.output_limit > self.limits.max_check_output_bytes:
            raise ValueError("Verification check exceeds current output policy")
        if not _command_is_application_owned(check):
            raise ValueError("Verification command does not match an application-owned template")

    async def _run_terminal(
        self,
        request: VerificationRequest,
        result: VerificationResult,
        check: VerificationCheck,
        timeout: float,
    ) -> object:
        command_started: float | None = None
        started = time.monotonic()
        backend = self.tools.sandbox
        task_policy = request.task.policy_context
        mode = task_policy.mode if task_policy is not None else AutonomyMode.SUPERVISED
        policy_fingerprint = (
            task_policy.policy_fingerprint
            if task_policy is not None else self.tools.policy.fingerprint
        )
        workspace = str(request.repository.root)
        policy_request = self.tools.policy.create_request(
            "terminal",
            {"command": shlex.join(check.argv), "cwd": check.cwd},
            mode=mode,
            task_id=request.task.task_id,
            step_id=check.check_id,
            backend_identity=backend.settings.execution_mode,
            workspace=workspace,
            policy_fingerprint=policy_fingerprint,
            category=OperationCategory.VERIFICATION_EXECUTION,
            workspace_valid=(
                backend.matches(request.session)
                and backend.workspace is not None
                and str(backend.workspace.resolve()) == workspace
                and (
                    task_policy is None
                    or task_policy.backend_identity == backend.settings.execution_mode
                    and task_policy.workspace_identity == workspace_fingerprint(workspace)
                )
            ),
            task_active=request.task.status == AgentStatus.VERIFYING,
        )
        decision = self.tools.policy.evaluate(
            policy_request,
            cancelled=bool(request.cancellation and request.cancellation.is_set()),
        )
        audit = PolicyAuditRecord(
            task_id=request.task.task_id,
            step_id=check.check_id,
            execution_id=None,
            tool_name="terminal",
            category=decision.category,
            mode=decision.mode,
            decision=decision.decision,
            reason=decision.reason,
            policy_fingerprint=decision.policy_fingerprint,
            approval_required=decision.decision == PolicyDecisionType.REQUIRE_APPROVAL,
            approval_outcome=(
                "pending"
                if decision.decision == PolicyDecisionType.REQUIRE_APPROVAL
                else "never_dispatched"
            ),
            backend_identity=backend.settings.execution_mode,
            workspace_identity=workspace_fingerprint(workspace),
            timestamp=datetime.now(timezone.utc).isoformat(),
        )
        audit.validate()
        request.task.policy_audit.append(audit)
        del request.task.policy_audit[:-512]

        def set_audit_outcome(value: str) -> None:
            for index in range(len(request.task.policy_audit) - 1, -1, -1):
                item = request.task.policy_audit[index]
                if item is audit:
                    request.task.policy_audit[index] = replace(
                        item, approval_outcome=value,
                    )
                    return

        async def approval_observer(
            name: str, description: str, decision: bool | None,
        ) -> None:
            nonlocal command_started
            del description
            if name != "terminal":
                raise ValueError("Verification approval observer received a non-terminal tool")
            if decision is None:
                result.approval_state = ApprovalStatus.PENDING
                request.task.transition(AgentStatus.WAITING_FOR_APPROVAL)
                await self._checkpoint(request.task)
                await self._emit(
                    request.task, "waiting_for_verification_approval",
                    check_id=check.check_id,
                )
            else:
                result.approval_state = ApprovalStatus.APPROVED if decision else ApprovalStatus.DENIED
                set_audit_outcome("approved" if decision else "denied")
                if decision:
                    command_started = time.monotonic()
                if request.task.status == AgentStatus.WAITING_FOR_APPROVAL:
                    request.task.resolve_approval(True)
                    await self._checkpoint(request.task)

        async def dispatch_guard() -> bool:
            if request.cancellation and request.cancellation.is_set():
                return False
            return (
                request.task.status == AgentStatus.VERIFYING
                and backend.matches(request.session)
                and backend.workspace is not None
                and str(backend.workspace.resolve()) == workspace
            )

        tool_task = asyncio.create_task(self.tools.call(
            "terminal",
            {"command": shlex.join(check.argv), "cwd": check.cwd},
            session=request.session,
            policy_request=policy_request,
            approval_observer=approval_observer,
            dispatch_guard=dispatch_guard,
        ))
        try:
            while not tool_task.done():
                if request.cancellation and request.cancellation.is_set():
                    tool_task.cancel()
                    await asyncio.gather(tool_task, return_exceptions=True)
                    raise asyncio.CancelledError
                remaining = (
                    timeout - (time.monotonic() - command_started)
                    if command_started is not None else 0.05
                )
                if command_started is not None and remaining <= 0:
                    tool_task.cancel()
                    await asyncio.gather(tool_task, return_exceptions=True)
                    raise TimeoutError
                await asyncio.wait({tool_task}, timeout=min(0.05, max(0.01, remaining)))
            tool_result = await tool_task
            if isinstance(tool_result, dict):
                if tool_result.get("error_code") == "POLICY_DENIED":
                    result.approval_state = ApprovalStatus.NOT_REQUIRED
                    set_audit_outcome("policy_denied")
                elif tool_result.get("error_code") == "CANCELLED":
                    result.approval_state = ApprovalStatus.NOT_REQUIRED
                    set_audit_outcome("cancelled")
                elif result.approval_state == ApprovalStatus.NOT_REQUIRED:
                    set_audit_outcome("not_required")
            return tool_result
        except BaseException:
            if not tool_task.done():
                tool_task.cancel()
                await asyncio.gather(tool_task, return_exceptions=True)
            raise
        finally:
            result.duration = max(0, time.monotonic() - started)

    def _apply_tool_result(
        self,
        record: VerificationResult,
        value: object,
        check: VerificationCheck,
        *,
        output_remaining: int,
    ) -> None:
        if not isinstance(value, dict):
            record.status = VerificationStatus.ERROR
            record.infrastructure_error = "Terminal tool returned a malformed result."
            record.repairability = Repairability.NOT_REPAIRABLE
            return
        record.duration = _duration(value.get("duration"))
        record.exit_code = value.get("exit_code") if type(value.get("exit_code")) is int else None
        stdout = value.get("stdout", "")
        stderr = value.get("stderr", "")
        if not isinstance(stdout, str):
            stdout = ""
        if not isinstance(stderr, str):
            stderr = ""
        combined_cap = min(check.output_limit, self.limits.max_check_output_bytes, output_remaining)
        stdout_cap = min(self.limits.max_stdout_bytes, combined_cap)
        record.stdout, out_truncated = _bound_utf8(stdout, stdout_cap)
        stderr_cap = min(
            self.limits.max_stderr_bytes,
            max(0, combined_cap - len(record.stdout.encode("utf-8"))),
        )
        record.stderr, err_truncated = _bound_utf8(stderr, stderr_cap)
        record.truncated = (
            value.get("truncated") is True or out_truncated or err_truncated
        )
        record.timed_out = value.get("timed_out") is True
        if value.get("denied") is True:
            record.status = VerificationStatus.BLOCKED
            record.approval_state = ApprovalStatus.DENIED
            record.infrastructure_error = str(value.get("error", "Terminal approval denied"))[:2048]
            record.repairability = Repairability.NOT_REPAIRABLE
        elif record.timed_out:
            record.status = VerificationStatus.TIMED_OUT
            record.infrastructure_error = str(value.get("error", "Verification command timed out"))[:2048]
            record.repairability = Repairability.NOT_REPAIRABLE
        elif record.exit_code is None:
            record.status = VerificationStatus.BLOCKED
            record.infrastructure_error = str(value.get("error", "Terminal execution is unavailable"))[:2048]
            record.repairability = Repairability.NOT_REPAIRABLE
        elif _is_missing_verifier(record.exit_code, record.stderr, check.argv):
            record.status = VerificationStatus.UNAVAILABLE
            record.infrastructure_error = _failure_excerpt(record.stderr or str(value.get("error", "")), self.limits.max_diagnostics)
            record.repairability = Repairability.NOT_REPAIRABLE
        elif record.exit_code == 0:
            record.status = VerificationStatus.PASSED
            record.repairability = Repairability.NOT_REPAIRABLE
        elif record.exit_code != 0:
            record.status = VerificationStatus.FAILED
            combined = "\n".join(part for part in (record.stdout, record.stderr) if part)
            record.failure_summary = _failure_excerpt(combined, self.limits.max_diagnostics)
            record.repairability = _repairability(
                record.failure_summary, combined, check.intent,
                truncated=record.truncated,
            )
        else:
            record.status = VerificationStatus.ERROR
            record.infrastructure_error = str(value.get("error", "Terminal tool reported an inconsistent result"))[:2048]
            record.repairability = Repairability.NOT_REPAIRABLE

    def _append_nonrun_result(
        self,
        task: AgentTask,
        check: VerificationCheck,
        status: VerificationStatus,
        message: str,
        *,
        repairability: Repairability,
    ) -> None:
        task.verification_results.append(VerificationResult(
            command=shlex.join(check.argv) if check.argv else check.display_name,
            status=status,
            check_id=check.check_id,
            intent=check.intent,
            verifier=check.project_type,
            cwd=check.cwd,
            required=check.required,
            relevant_paths=check.relevant_paths,
            failure_summary=message[:4096] if status == VerificationStatus.SKIPPED else None,
            infrastructure_error=message[:2048] if status != VerificationStatus.SKIPPED else None,
            repairability=repairability,
            run_id=(
                task.verification_plan.run_id
                if task.verification_plan is not None else None
            ),
        ))

    async def _cancel_before_start(self, task: AgentTask) -> VerificationRunResult:
        task.verification_outcome = VerificationOutcome.CANCELLED
        task.transition(AgentStatus.CANCELLED)
        task.terminal_summary = "Verification cancelled before planning."
        await self._checkpoint(task)
        await self._emit(task, "verification_cancelled")
        return self._result(task, VerificationOutcome.CANCELLED, error="Cancelled before verification started")

    async def _cancel_run(
        self,
        task: AgentTask,
        check_id: str | None,
        *,
        existing: VerificationResult | None = None,
    ) -> VerificationRunResult:
        active = existing or next(
            (
                result for result in reversed(task.verification_results)
                if result.check_id == check_id and result.status == VerificationStatus.RUNNING
            ),
            None,
        )
        if active is not None:
            waiting_approval = active.approval_state == ApprovalStatus.PENDING
            active.status = (
                VerificationStatus.CANCELLED if waiting_approval
                else VerificationStatus.INTERRUPTED
            )
            active.infrastructure_error = (
                "Verification was cancelled while awaiting approval; command was not dispatched."
                if waiting_approval
                else "Verification was cancelled; command outcome is uncertain and will not be replayed."
            )
            active.repairability = Repairability.NOT_REPAIRABLE
        task.current_verification_check_id = None
        task.verification_outcome = VerificationOutcome.CANCELLED
        if task.status == AgentStatus.WAITING_FOR_APPROVAL:
            task.status = AgentStatus.CANCELLED
            task.approval_resume_state = None
        elif task.status != AgentStatus.CANCELLED:
            task.transition(AgentStatus.CANCELLED)
        task.terminal_summary = "Verification cancelled; uncertain commands will not be replayed."
        await self._checkpoint(task)
        await self._emit(task, "verification_cancelled", check_id=check_id)
        return self._result(
            task, VerificationOutcome.CANCELLED,
            error="Verification cancelled; current command outcome is uncertain",
        )

    async def _checkpoint(self, task: AgentTask) -> None:
        if self.checkpoint is not None:
            await self.checkpoint(AgentCheckpoint(task))

    async def _record_error(
        self,
        task: AgentTask,
        message: str,
        *,
        preserve: bool,
    ) -> VerificationRunResult:
        details = message[:2048]
        if not preserve and task.status in {
            AgentStatus.VERIFYING, AgentStatus.WAITING_FOR_APPROVAL,
        }:
            if task.status == AgentStatus.WAITING_FOR_APPROVAL:
                task.status = AgentStatus.VERIFYING
                task.approval_resume_state = None
            for result in task.verification_results:
                if result.status == VerificationStatus.RUNNING:
                    result.status = VerificationStatus.INTERRUPTED
                    result.infrastructure_error = (
                        "Verification orchestration failed while this command was active; "
                        "its outcome is uncertain and it will not be replayed."
                    )
                    result.repairability = Repairability.NOT_REPAIRABLE
            task.current_verification_check_id = None
            task.verification_outcome = VerificationOutcome.ERROR
            task.terminal_summary = f"Verification error: {message[:1024]}"
            try:
                await self._checkpoint(task)
            except Exception as exc:
                details += f"; checkpoint failed: {str(exc)[:512]}"
            try:
                await self._emit(task, "verification_error", message=message[:1024])
            except Exception as exc:
                details += f"; event delivery failed: {str(exc)[:512]}"
        return self._result(task, VerificationOutcome.ERROR, error=details)

    async def _emit(
        self,
        task: AgentTask,
        kind: str,
        *,
        check_id: str | None = None,
        message: str | None = None,
    ) -> None:
        if self.event_sink is None:
            return
        from synai.coding_agent.runtime import RuntimeEvent

        try:
            await self.event_sink(RuntimeEvent(
                kind=kind,
                task_id=task.task_id,
                state=task.status,
                step_id=check_id,
                tool_name="terminal" if check_id else None,
                message=message[:4096] if message else None,
            ))
        except Exception as exc:
            _logger.warning(
                "Observer event delivery failed for task %s event %s: %s",
                task.task_id, kind, str(exc)[:512],
            )

    @staticmethod
    def _event_for_status(status: VerificationStatus) -> str:
        return {
            VerificationStatus.PASSED: "check_passed",
            VerificationStatus.FAILED: "check_failed",
            VerificationStatus.TIMED_OUT: "check_timed_out",
            VerificationStatus.CANCELLED: "check_blocked",
            VerificationStatus.UNAVAILABLE: "check_blocked",
            VerificationStatus.BLOCKED: "check_blocked",
            VerificationStatus.ERROR: "check_blocked",
            VerificationStatus.SKIPPED: "check_blocked",
            VerificationStatus.INTERRUPTED: "check_blocked",
            VerificationStatus.RUNNING: "check_started",
        }[status]

    @staticmethod
    def _overall(
        plan: VerificationPlan,
        results: list[VerificationResult],
    ) -> VerificationOutcome:
        current = [
            item for item in results
            if item.run_id == plan.run_id
            and item.check_id in {check.check_id for check in plan.checks}
        ]
        if any(item.status == VerificationStatus.CANCELLED for item in current):
            return VerificationOutcome.CANCELLED
        if any(item.status == VerificationStatus.ERROR for item in current):
            return VerificationOutcome.ERROR
        if any(item.required and item.status in {
            VerificationStatus.BLOCKED, VerificationStatus.UNAVAILABLE,
            VerificationStatus.TIMED_OUT, VerificationStatus.INTERRUPTED,
        } for item in current):
            return VerificationOutcome.BLOCKED
        if plan.unsupported_intents:
            return VerificationOutcome.BLOCKED
        if any(item.status == VerificationStatus.RUNNING for item in current):
            return VerificationOutcome.UNKNOWN
        failed = [
            item for item in current
            if item.required and item.status == VerificationStatus.FAILED
        ]
        if failed and any(
            item.repairability not in {
                Repairability.CODE_REPAIR_CANDIDATE,
                Repairability.CONFIGURATION_REPAIR_CANDIDATE,
            }
            for item in failed
        ):
            return VerificationOutcome.BLOCKED
        if failed:
            return VerificationOutcome.CODE_FAILURE
        if any(item.status == VerificationStatus.PASSED for item in current):
            return VerificationOutcome.PASSED
        return VerificationOutcome.BLOCKED

    @staticmethod
    def _render_plan(plan: VerificationPlan) -> str:
        lines = [f"Project: {plan.project_type}"]
        lines.extend(f"Root: {root}" for root in plan.project_roots)
        for check in plan.checks:
            command = shlex.join(check.argv) if check.argv else "unavailable"
            lines.append(
                f"{check.check_id} [{check.intent.value}] {check.display_name}: {command}"
                + ("" if check.available else f" (unavailable: {check.discovery_reason})")
            )
        return "\n".join(lines)[:16_384]

    @staticmethod
    def _result(
        task: AgentTask,
        outcome: VerificationOutcome,
        *,
        ready_for_review: bool = False,
        repair_required: bool = False,
        no_verification_needed: bool = False,
        error: str | None = None,
    ) -> VerificationRunResult:
        plan = task.verification_plan
        return VerificationRunResult(
            task_id=task.task_id,
            state=task.status,
            outcome=outcome,
            plan=plan,
            results=tuple(task.verification_results),
            ready_for_review=ready_for_review,
            repair_required=repair_required,
            no_verification_needed=no_verification_needed,
            error=error[:2048] if error else None,
        )


def _discover_projects(
    root: Path,
    limits: VerificationLimits,
    cancellation: threading.Event | None,
) -> tuple[list[_Project], tuple[str, ...], list[str]]:
    manifests: dict[str, set[str]] = {}
    test_paths: list[str] = []
    python_test_files: list[tuple[str, str]] = []
    warnings: list[str] = []
    stack: list[tuple[str, int]] = [(".", 0)]
    scanned = 0
    started = time.monotonic()
    while stack:
        if cancellation and cancellation.is_set():
            raise InterruptedError("Cancelled during bounded project discovery")
        if scanned >= limits.max_discovery_entries or time.monotonic() - started > limits.max_discovery_seconds:
            warnings.append("Project discovery reached its configured scan bound.")
            break
        relative_directory, depth = stack.pop()
        directory_fd: int | None = None
        stop_discovery = False
        try:
            directory_fd = _open_workspace_directory(root, relative_directory)
            with os.scandir(directory_fd) as iterator:
                names = []
                remaining_entries = limits.max_discovery_entries - scanned
                for entry in iterator:
                    names.append(entry.name)
                    if (
                        len(names) > remaining_entries
                        or time.monotonic() - started > limits.max_discovery_seconds
                    ):
                        warnings.append("Project discovery reached its configured scan bound.")
                        names.clear()
                        stop_discovery = True
                        break
            names.sort(reverse=True)
        except OSError:
            if directory_fd is not None:
                os.close(directory_fd)
            continue
        if stop_discovery:
            if directory_fd is not None:
                os.close(directory_fd)
            break
        try:
            for name in names:
                if time.monotonic() - started > limits.max_discovery_seconds:
                    warnings.append("Project discovery reached its configured scan bound.")
                    break
                scanned += 1
                if scanned > limits.max_discovery_entries:
                    break
                relative = (
                    f"{relative_directory}/{name}"
                    if relative_directory != "." else name
                )
                if not _safe_relative(relative):
                    continue
                try:
                    info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                    if stat.S_ISLNK(info.st_mode):
                        continue
                    if stat.S_ISDIR(info.st_mode):
                        if depth < 12 and name not in _EXCLUDED_DIRS:
                            stack.append((relative, depth + 1))
                        continue
                    if not stat.S_ISREG(info.st_mode):
                        continue
                except OSError:
                    continue
                path = root.joinpath(*PurePosixPath(relative).parts)
                if _test_candidate(root, relative, path):
                    if path.suffix.lower() in {".py", ".js", ".mjs", ".cjs", ".ts", ".tsx"}:
                        if len(test_paths) < 512:
                            test_paths.append(relative)
                        else:
                            if "Test-path discovery reached its 512-path bound." not in warnings:
                                warnings.append("Test-path discovery reached its 512-path bound.")
                        if path.suffix.lower() == ".py" and len(python_test_files) < 512:
                            content = _read_workspace_text(root, relative, 32_768) or ""
                            python_test_files.append((relative, content))
                if (
                    name in {
                        "pyproject.toml", "setup.cfg", "tox.ini", "pytest.ini", "package.json",
                        "tsconfig.json", "Cargo.toml", "ruff.toml", "pyrightconfig.json",
                        "mypy.ini", ".mypy.ini", "setup.py",
                    }
                    or name.startswith("requirements") and name.endswith(".txt")
                ):
                    project_root = relative_directory
                    manifests.setdefault(project_root, set()).add(name)
        finally:
            if directory_fd is not None:
                os.close(directory_fd)
    projects: list[_Project] = []
    for project_root, names in sorted(manifests.items()):
        if time.monotonic() - started > limits.max_discovery_seconds:
            warnings.append("Project metadata parsing reached its configured scan bound.")
            break
        if len(projects) >= 64:
            warnings.append("Project metadata parsing reached its 64-project bound.")
            break
        python_manifest = bool(names & {
            "pyproject.toml", "setup.cfg", "tox.ini", "pytest.ini", "ruff.toml",
            "pyrightconfig.json", "mypy.ini", ".mypy.ini", "setup.py",
        }) or any(name.startswith("requirements") and name.endswith(".txt") for name in names)
        kinds = {
            kind for present, kind in (
                (python_manifest, "python"),
                ("Cargo.toml" in names, "rust"),
                ("package.json" in names, "node"),
            )
            if present
        }
        if len(kinds) > 1:
            kind = "unknown"
            warnings.append(
                f"Ambiguous project manifests at {project_root}; no verifier will be guessed.",
            )
        elif kinds:
            kind = next(iter(kinds))
        else:
            kind = "typescript"
        manifest: dict[str, Any] = {}
        evidence = set(names)
        if "pyproject.toml" in names:
            try:
                source = _read_workspace_text(root, f"{project_root}/pyproject.toml" if project_root != "." else "pyproject.toml", 262_144)
                if source is None:
                    raise ValueError("Manifest is missing, oversized, or not UTF-8")
                data = tomllib.loads(source)
                if isinstance(data, dict):
                    manifest = data
            except (OSError, UnicodeError, ValueError, tomllib.TOMLDecodeError):
                warnings.append(f"Could not parse {project_root}/pyproject.toml.")
        elif "package.json" in names:
            try:
                source = _read_workspace_text(root, f"{project_root}/package.json" if project_root != "." else "package.json", 262_144)
                if source is None:
                    raise ValueError("Manifest is missing, oversized, or not UTF-8")
                data = json.loads(source)
                if isinstance(data, dict):
                    manifest = data
            except (OSError, UnicodeError, ValueError):
                warnings.append(f"Could not parse {project_root}/package.json.")
        elif "Cargo.toml" in names:
            try:
                source = _read_workspace_text(root, f"{project_root}/Cargo.toml" if project_root != "." else "Cargo.toml", 262_144)
                if source is None:
                    raise ValueError("Manifest is missing, oversized, or not UTF-8")
                data = tomllib.loads(source)
                if isinstance(data, dict):
                    manifest = data
            except (OSError, UnicodeError, ValueError, tomllib.TOMLDecodeError):
                warnings.append(f"Could not parse {project_root}/Cargo.toml.")
        project_tests = tuple(sorted(
            path for path in test_paths
            if project_root == "." or path.startswith(project_root + "/")
        ))
        project_test_sources = [
            content for path, content in python_test_files
            if project_root == "." or path.startswith(project_root + "/")
        ]
        unittest_style = any(
            "unittest.TestCase" in content or re.search(r"from unittest import|import unittest", content)
            for content in project_test_sources
        )
        setup_cfg_pytest = False
        if "setup.cfg" in names:
            setup_content = _read_workspace_text(
                root,
                f"{project_root}/setup.cfg" if project_root != "." else "setup.cfg",
                262_144,
            )
            if setup_content is not None:
                setup_cfg_pytest = bool(re.search(r"(?m)^\s*\[tool:pytest\]\s*$", setup_content))
        tox_pytest = False
        if "tox.ini" in names:
            tox_source = _read_workspace_text(
                root,
                f"{project_root}/tox.ini" if project_root != "." else "tox.ini",
                262_144,
            )
            tox_pytest = tox_source is not None and "pytest" in tox_source
        pytest_configured = (
            "pytest.ini" in names or tox_pytest
            or setup_cfg_pytest
            or isinstance(manifest.get("tool"), dict) and "pytest" in manifest.get("tool", {})
            or isinstance(manifest.get("tool"), dict)
            and isinstance(manifest["tool"].get("pytest"), dict)
            or _declares_tool(root, project_root, manifest, "pytest", names)
        )
        if kind == "python" and project_test_sources:
            evidence.add("python-tests")
        for requirement_file in names:
            if requirement_file.startswith("requirements") and requirement_file.endswith(".txt"):
                evidence.add(requirement_file)
        projects.append(_Project(
            project_root, kind, manifest, frozenset(evidence),
            project_tests, unittest_style, bool(pytest_configured),
        ))
    return projects, tuple(sorted(set(test_paths))[:512]), warnings


def _declares_tool(
    workspace: Path,
    project_root: str,
    manifest: dict[str, Any],
    name: str,
    filenames: set[str],
) -> bool:
    prefix = "" if project_root == "." else project_root + "/"
    if name in {"ruff", "mypy", "pyright"}:
        tool_config = manifest.get("tool")
        if isinstance(tool_config, dict) and name in tool_config:
            return True
        if name == "ruff" and _read_workspace_text(
            workspace, f"{prefix}ruff.toml", 262_144,
        ) is not None:
            return True
        if name == "pyright" and _read_workspace_text(
            workspace, f"{prefix}pyrightconfig.json", 262_144,
        ) is not None:
            return True
        if name == "mypy" and any(
            _read_workspace_text(workspace, f"{prefix}{filename}", 262_144) is not None
            for filename in ("mypy.ini", ".mypy.ini")
        ):
            return True
    dependencies: list[str] = []
    project = manifest.get("project")
    if isinstance(project, dict):
        for field in ("dependencies", "optional-dependencies"):
            value = project.get(field)
            if isinstance(value, list):
                dependencies.extend(item for item in value if isinstance(item, str))
            elif isinstance(value, dict):
                dependencies.extend(
                    item for group in value.values() if isinstance(group, list)
                    for item in group if isinstance(item, str)
                )
    for field in ("dev-dependencies", "dependency-groups"):
        value = manifest.get(field)
        if isinstance(value, dict):
            dependencies.extend(
                item for group in value.values() if isinstance(group, list)
                for item in group if isinstance(item, str)
            )
    if name in {"ruff", "mypy", "pyright", "pytest"}:
        if any(re.match(rf"^\s*{re.escape(name)}(?:\s|[<>=!~;\[]|$)", item, re.I) for item in dependencies):
            return True
        candidates = {
            candidate for candidate in filenames
            if candidate.startswith("requirements") and candidate.endswith(".txt")
        }
        for candidate in sorted(candidates):
            content = _read_workspace_text(
                workspace, f"{prefix}{candidate}", 262_144,
            )
            if content is not None:
                if any(re.match(
                        rf"^\s*{re.escape(name)}(?:\s|[<>=!~;\[]|$)",
                        line.split("#", 1)[0], re.I,
                    ) for line in content.splitlines()):
                    return True
    return False


def _fallback_python(root: Path, tests: tuple[str, ...]) -> _Project:
    unittest_style = False
    for path in tests[:32]:
        content = _read_workspace_text(root, path, 32_768)
        if content is None:
            continue
        if "unittest.TestCase" in content or re.search(r"from unittest import|import unittest", content):
            unittest_style = True
            break
    return _Project(
        ".", "python", {}, frozenset({"python-source"}),
        tests, unittest_style, False,
    )


def _projects_for_paths(projects: list[_Project], paths: tuple[str, ...]) -> list[_Project]:
    selected: dict[str, _Project] = {}
    for relative in paths:
        if not _safe_relative(relative):
            continue
        matches = [
            project for project in projects
            if project.root == "." or relative == project.root or relative.startswith(project.root + "/")
        ]
        if matches:
            project = max(matches, key=lambda item: len(PurePosixPath(item.root).parts))
            selected[project.root] = project
    return list(selected.values())


def _test_targets(
    projects: list[_Project],
    scope_paths: tuple[str, ...],
    request: VerificationRequest,
    limits: VerificationLimits,
) -> dict[str, tuple[str, ...]]:
    candidates: dict[str, set[str]] = {project.root: set() for project in projects}
    declared_tests = {
        path for path in scope_paths if _looks_like_test(path)
    }
    context_tests = set()
    if request.context is not None:
        context_tests.update(
            item.path for item in request.context.items
            if item.kind == ContextKind.TEST and item.path is not None and _looks_like_test(item.path)
        )
    for project in projects:
        for test in project.test_paths:
            if test in declared_tests or test in context_tests:
                candidates[project.root].add(test)
        source_paths = {
            path for path in scope_paths
            if path.endswith(".py") and not _looks_like_test(path)
            and (project.root == "." or path.startswith(project.root + "/"))
        }
        stems = {PurePosixPath(path).stem for path in source_paths}
        for test in project.test_paths:
            stem = PurePosixPath(test).stem
            tested = stem.removeprefix("test_").removesuffix("_test")
            if tested in stems:
                candidates[project.root].add(test)
    output: dict[str, tuple[str, ...]] = {}
    for project in projects:
        ordered = sorted(candidates[project.root])
        output[project.root] = tuple(
            ordered[:max(limits.max_targeted_paths, limits.max_relevant_paths)]
        )
    return output


def _checks_for_intent(
    intent: VerificationIntent,
    project: _Project,
    workspace: Path,
    scope_paths: tuple[str, ...],
    test_targets: tuple[str, ...],
    limits: VerificationLimits,
) -> list[VerificationCheck]:
    root = project.root
    relative_paths = tuple(sorted(
        path for path in scope_paths
        if project.root == "." or path == project.root or path.startswith(project.root + "/")
    ))
    local_paths = tuple(
        path[len(root) + 1:] if root != "." and path.startswith(root + "/") else path
        for path in relative_paths
    )
    test_local = tuple(
        path[len(root) + 1:] if root != "." and path.startswith(root + "/") else path
        for path in test_targets
    )
    project_cwd = root
    existing_local = tuple(
        path for path in local_paths
        if _is_existing_file(workspace / project_cwd, path)
    )
    python_paths = tuple(path for path in existing_local if path.endswith(".py"))
    if project.kind == "python":
        if intent == VerificationIntent.SYNTAX_CHECK:
            if python_paths:
                return [_make_check(intent, project, ("python3", "-m", "compileall", "-q", "--", *map(_argument_path, python_paths)), project_cwd, python_paths, limits, "Compile only changed/declaration-scoped Python files.")]
            return [_unavailable(intent, project, project_cwd, "No in-scope Python source files were discovered.", limits)]
        if intent in {VerificationIntent.TARGETED_TESTS, VerificationIntent.RELEVANT_TESTS}:
            if not project.pytest_configured and not project.unittest_style:
                return [_unavailable(intent, project, project_cwd, "No configured pytest dependency/configuration or unittest-style tests were detected.", limits)]
            if not test_local:
                return [_unavailable(intent, project, project_cwd, "No explicitly declared or deterministically related test paths were found.", limits)]
            if project.pytest_configured:
                argv = ("python3", "-m", "pytest", "-q", "--", *test_local)
            else:
                groups: dict[tuple[str, str], list[str]] = {}
                for path in test_local:
                    parent = PurePosixPath(path).parent.as_posix()
                    pattern = PurePosixPath(path).name
                    groups.setdefault((parent if parent != "." else ".", pattern), []).append(path)
                return [
                    _make_check(
                        intent, project,
                        ("python3", "-m", "unittest", "discover", "-s", parent, "-p", pattern),
                        project_cwd, tuple(paths), limits,
                        "Use unittest discovery for explicitly selected test files.",
                    )
                    for (parent, pattern), paths in sorted(groups.items())
                ]
            return [_make_check(intent, project, argv, project_cwd, test_local, limits, "Run only deterministically selected test paths.")]
        if intent == VerificationIntent.FULL_TEST_SUITE:
            if project.pytest_configured:
                return [_make_check(intent, project, ("python3", "-m", "pytest", "-q"), project_cwd, (), limits, "pytest configuration/dependency explicitly identifies the test runner.")]
            if project.unittest_style:
                test_start = next(
                    (
                        directory for directory in ("tests", "test")
                        if (workspace / project_cwd / directory).is_dir()
                        and not (workspace / project_cwd / directory).is_symlink()
                    ),
                    ".",
                )
                return [_make_check(intent, project, ("python3", "-m", "unittest", "discover", "-s", test_start), project_cwd, (), limits, "Detected unittest.TestCase/import conventions in project tests.")]
            return [_unavailable(intent, project, project_cwd, "No deterministic full-suite runner evidence was found.", limits)]
        if intent == VerificationIntent.LINT:
            if _declares_tool(
                workspace, project.root, project.manifest, "ruff", set(project.evidence),
            ):
                args = python_paths or tuple(path for path in existing_local if _safe_relative(path))
                if not args:
                    return [_unavailable(intent, project, project_cwd, "No existing in-scope Python files are available for linting.", limits)]
                return [_make_check(intent, project, ("ruff", "check", "--", *map(_argument_path, args)), project_cwd, args, limits, "Ruff is explicitly configured or declared.")]
            return [_unavailable(intent, project, project_cwd, "Ruff is neither configured nor declared by project dependency metadata.", limits)]
        if intent == VerificationIntent.TYPE_CHECK:
            checker = next(
                (name for name in ("mypy", "pyright") if _declares_tool(
                    workspace, project.root, project.manifest, name, set(project.evidence),
                )),
                None,
            )
            if checker is None:
                return [_unavailable(intent, project, project_cwd, "No configured or declared Python type checker was found.", limits)]
            args = python_paths or tuple(path for path in existing_local if _safe_relative(path))
            if not args:
                return [_unavailable(intent, project, project_cwd, "No existing in-scope Python files are available for type checking.", limits)]
            argv = ("mypy", "--", *map(_argument_path, args)) if checker == "mypy" else ("pyright", *map(_argument_path, args))
            return [_make_check(intent, project, argv, project_cwd, args, limits, f"{checker} is explicitly configured or declared.")]
        if intent == VerificationIntent.BUILD:
            if isinstance(project.manifest.get("build-system"), dict):
                return [_make_check(intent, project, ("python3", "-m", "build"), project_cwd, (), limits, "pyproject.toml declares a PEP 517 build-system.")]
            return [_unavailable(intent, project, project_cwd, "No configured Python build-system was found.", limits)]
    if project.kind in {"node", "typescript"}:
        manifest = project.manifest
        scripts = manifest.get("scripts", {}) if isinstance(manifest, dict) else {}
        scripts = scripts if isinstance(scripts, dict) else {}
        manager = _node_manager(workspace / project_cwd, manifest)
        if intent in {VerificationIntent.TARGETED_TESTS, VerificationIntent.RELEVANT_TESTS, VerificationIntent.FULL_TEST_SUITE}:
            script = scripts.get("test")
            if not isinstance(script, str) or manager is None:
                return [_unavailable(intent, project, project_cwd, "A package test script and an explicit package-manager lock/configuration are required.", limits)]
            if intent != VerificationIntent.FULL_TEST_SUITE and not test_local:
                return [_unavailable(intent, project, project_cwd, "No explicitly declared or deterministically related test paths were found.", limits)]
            suffix = test_local if intent != VerificationIntent.FULL_TEST_SUITE else ()
            argv = (manager, "run", "test", *(("--", *suffix) if suffix else ()))
            return [_make_check(intent, project, argv, project_cwd, test_local, limits, "package.json defines a test script and package manager evidence is present.")]
        if intent == VerificationIntent.LINT:
            script = "lint" if isinstance(scripts.get("lint"), str) else None
            if script is None or manager is None:
                return [_unavailable(intent, project, project_cwd, "No configured package lint script/package manager was found.", limits)]
            return [_make_check(intent, project, (manager, "run", script), project_cwd, (), limits, "Run the explicit package.json lint script.")]
        if intent == VerificationIntent.BUILD:
            if not isinstance(scripts.get("build"), str) or manager is None:
                return [_unavailable(intent, project, project_cwd, "No configured package build script/package manager was found.", limits)]
            return [_make_check(intent, project, (manager, "run", "build"), project_cwd, (), limits, "Run the explicit package.json build script.")]
        if intent == VerificationIntent.TYPE_CHECK:
            type_script = next(
                (name for name in ("typecheck", "type-check", "check-types") if isinstance(scripts.get(name), str)),
                None,
            )
            if type_script is not None and manager is not None:
                return [_make_check(intent, project, (manager, "run", type_script), project_cwd, (), limits, "Run the explicit package type-check script.")]
            if (
                "tsconfig.json" in project.evidence
                and manager is not None
                and _node_dependency(manifest, "typescript")
            ):
                return [_make_check(intent, project, ("node_modules/.bin/tsc", "--noEmit"), project_cwd, (), limits, "TypeScript config and dependency are explicit; use only the local compiler binary.")]
            return [_unavailable(intent, project, project_cwd, "No configured type-check script or local TypeScript compiler evidence was found.", limits)]
        if intent == VerificationIntent.SYNTAX_CHECK:
            js_paths = tuple(path for path in existing_local if path.endswith((".js", ".mjs", ".cjs")))
            if js_paths:
                return [_make_check(intent, project, ("node", "--check", *js_paths), project_cwd, js_paths, limits, "Node syntax check applies only to changed JavaScript files.")]
            return [_unavailable(intent, project, project_cwd, "No in-scope JavaScript files support a deterministic Node syntax check.", limits)]
    if project.kind == "rust":
        if intent == VerificationIntent.FULL_TEST_SUITE:
            return [_make_check(intent, project, ("cargo", "test", "--workspace"), project_cwd, (), limits, "Cargo.toml identifies a Rust workspace; full test intent was explicit.")]
        if intent in {VerificationIntent.TARGETED_TESTS, VerificationIntent.RELEVANT_TESTS}:
            return [_unavailable(intent, project, project_cwd, "No safe deterministic mapping from plan paths to individual Rust tests is available.", limits)]
        if intent == VerificationIntent.TYPE_CHECK:
            return [_make_check(intent, project, ("cargo", "check", "--workspace"), project_cwd, (), limits, "Cargo check is the deterministic Rust type/build analysis for this intent.")]
        if intent == VerificationIntent.BUILD:
            return [_make_check(intent, project, ("cargo", "build", "--workspace"), project_cwd, (), limits, "Cargo.toml identifies a Rust workspace and build intent was explicit.")]
        if intent == VerificationIntent.LINT:
            return [_make_check(intent, project, ("cargo", "clippy", "--workspace", "--all-targets", "--", "-D", "warnings"), project_cwd, (), limits, "Clippy is selected only for an explicit lint intent.")]
        if intent == VerificationIntent.SYNTAX_CHECK:
            return [_make_check(intent, project, ("cargo", "check", "--workspace"), project_cwd, (), limits, "Cargo check provides the supported Rust syntax/type validation.")]
    return [_unavailable(
        intent, project, project_cwd,
        f"No supported deterministic {intent.value} verifier is configured for project type {project.kind}.",
        limits,
    )]


def _make_check(
    intent: VerificationIntent,
    project: _Project,
    argv: tuple[str, ...],
    cwd: str,
    paths: tuple[str, ...],
    limits: VerificationLimits,
    reason: str,
) -> VerificationCheck:
    slug = (re.sub(r"[^a-z0-9]+", "-", project.root.lower()).strip("-") or "root")[:64]
    command_length = len(shlex.join(argv))
    available = command_length <= 4096
    if not available:
        reason = "Generated verifier arguments exceeded the bounded command length."
        argv = ()
    fingerprint = hashlib.sha256(
        json.dumps([project.root, *argv], ensure_ascii=True).encode("utf-8"),
    ).hexdigest()[:8]
    check_id = f"check-{intent.value.replace('_', '-')}-{slug}-{fingerprint}"
    return VerificationCheck(
        check_id=check_id,
        intent=intent,
        project_type=project.kind,
        display_name=f"{intent.value.replace('_', ' ').title()} ({project.root[:160]})",
        argv=argv,
        cwd=cwd,
        relevant_paths=tuple(paths[:64]),
        required=True,
        available=available,
        timeout_seconds=min(limits.max_command_seconds, 300),
        output_limit=limits.max_check_output_bytes,
        discovery_reason=reason,
    )


def _unavailable(
    intent: VerificationIntent,
    project: _Project,
    cwd: str,
    reason: str,
    limits: VerificationLimits,
) -> VerificationCheck:
    slug = (re.sub(r"[^a-z0-9]+", "-", project.root.lower()).strip("-") or "root")[:64]
    return VerificationCheck(
        check_id=f"check-{intent.value.replace('_', '-')}-{slug}",
        intent=intent,
        project_type=project.kind,
        display_name=f"{intent.value.replace('_', ' ').title()} ({project.root[:160]})",
        argv=(),
        cwd=cwd,
        relevant_paths=(),
        required=True,
        available=False,
        timeout_seconds=min(limits.max_command_seconds, 300),
        output_limit=limits.max_check_output_bytes,
        discovery_reason=reason,
    )


def _node_manager(root: Path, manifest: dict[str, Any]) -> str | None:
    package_manager = manifest.get("packageManager")
    if isinstance(package_manager, str):
        name = package_manager.split("@", 1)[0]
        if name in {"npm", "pnpm", "yarn"}:
            return name
    for name, manager in (
        ("package-lock.json", "npm"), ("npm-shrinkwrap.json", "npm"),
        ("pnpm-lock.yaml", "pnpm"), ("yarn.lock", "yarn"),
    ):
        path = root / name
        if path.is_file() and not path.is_symlink():
            return manager
    return None


def _node_dependency(manifest: dict[str, Any], dependency: str) -> bool:
    for field in ("dependencies", "devDependencies", "peerDependencies"):
        value = manifest.get(field)
        if isinstance(value, dict) and dependency in value:
            return True
    return False


def _looks_like_test(path: str) -> bool:
    name = PurePosixPath(path).name
    return (
        name.startswith(("test_", "test."))
        or name.endswith(("_test.py", ".test.ts", ".test.tsx", ".test.js", ".spec.ts", ".spec.js"))
        or "/tests/" in f"/{path}/"
    )


def _test_candidate(root: Path, relative: str, path: Path) -> bool:
    name = path.name.lower()
    if (
        name.startswith(("test_", "test."))
        or name.endswith(("_test.py", ".test.ts", ".test.tsx", ".test.js", ".spec.ts", ".spec.js"))
    ):
        return True
    if any(part in {"tests", "__tests__"} for part in PurePosixPath(relative).parts):
        if path.suffix.lower() == ".py":
            content = _read_workspace_text(root, relative, 32_768)
            if content is None:
                return False
            return bool(re.search(
                r"\bdef\s+test_|unittest\.TestCase|\bclass\s+Test[A-Z_]",
                content,
            ))
    return False


def _safe_relative(path: str) -> bool:
    if not isinstance(path, str) or not path or "\\" in path or "\x00" in path:
        return False
    if any(ord(character) < 32 or ord(character) == 127 for character in path):
        return False
    parsed = PurePosixPath(path)
    return (
        not parsed.is_absolute() and parsed.as_posix() == path
        and all(part not in {"", ".", ".."} for part in parsed.parts)
    )


def _open_workspace_directory(root: Path, relative: str) -> int:
    flags = (
        os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptor = os.open(root, flags)
    try:
        if relative == ".":
            return descriptor
        for component in PurePosixPath(relative).parts:
            next_descriptor = os.open(component, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = next_descriptor
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _read_workspace_text(root: Path, relative: str, maximum: int) -> str | None:
    if not _safe_relative(relative) or type(maximum) is not int or maximum < 0:
        return None
    directory_fd: int | None = None
    file_fd: int | None = None
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    directory_flags = flags | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    file_flags = flags | getattr(os, "O_NOFOLLOW", 0)
    try:
        directory_fd = os.open(root, directory_flags)
        parts = PurePosixPath(relative).parts
        for component in parts[:-1]:
            next_fd = os.open(component, directory_flags, dir_fd=directory_fd)
            os.close(directory_fd)
            directory_fd = next_fd
        file_fd = os.open(parts[-1], file_flags, dir_fd=directory_fd)
        info = os.fstat(file_fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > maximum:
            return None
        data = bytearray()
        while len(data) <= maximum:
            block = os.read(file_fd, min(65_536, maximum + 1 - len(data)))
            if not block:
                break
            data.extend(block)
        if len(data) > maximum:
            return None
        return data.decode("utf-8")
    except (OSError, UnicodeError, ValueError):
        return None
    finally:
        if file_fd is not None:
            os.close(file_fd)
        if directory_fd is not None:
            os.close(directory_fd)


def _argument_path(path: str) -> str:
    return f"./{path}" if PurePosixPath(path).name.startswith("-") else path


def _is_existing_file(root: Path, relative: str) -> bool:
    try:
        return _validate_relative(root, relative).is_file()
    except (OSError, ValueError):
        return False


def _validate_relative(root: Path, relative: str, *, allow_missing: bool = False, allow_root: bool = False) -> Path:
    if allow_root and relative == ".":
        target = root
    elif not _safe_relative(relative):
        raise ValueError(f"Unsafe workspace-relative verification path: {relative!r}")
    else:
        target = root
        for part in PurePosixPath(relative).parts:
            target = target / part
            try:
                if target.is_symlink():
                    raise ValueError(f"Symlink path is not allowed for verification: {relative}")
            except OSError as exc:
                raise ValueError(f"Cannot validate verification path {relative!r}: {exc}") from exc
    if not target.resolve(strict=False).is_relative_to(root):
        raise ValueError(f"Verification path escapes the active workspace: {relative}")
    if not allow_missing and not target.exists():
        raise ValueError(f"Verification path is no longer available: {relative}")
    return target


def _command_is_application_owned(check: VerificationCheck) -> bool:
    argv = check.argv
    if not argv:
        return False
    safe_paths = lambda values: all(_safe_relative(value) for value in values)
    if check.project_type == "python":
        if argv[:5] == ("python3", "-m", "compileall", "-q", "--"):
            return len(argv) > 5 and safe_paths(argv[5:])
        if argv[:4] == ("python3", "-m", "pytest", "-q"):
            return (
                len(argv) == 4
                or len(argv) > 5 and argv[4] == "--" and safe_paths(argv[5:])
            )
        if argv[:3] == ("python3", "-m", "unittest"):
            if argv[3:4] == ("discover",):
                return (
                    len(argv) in {6, 8}
                    and argv[4] == "-s"
                    and (argv[5] == "." or _safe_relative(argv[5]))
                    and (
                        len(argv) == 6
                        or argv[6] == "-p" and _safe_relative(argv[7])
                    )
                )
        if argv == ("python3", "-m", "build"):
            return True
        if argv[:2] == ("ruff", "check"):
            return len(argv) >= 4 and argv[2] == "--" and safe_paths(argv[3:])
        if argv[:2] == ("mypy", "--"):
            return len(argv) > 2 and safe_paths(argv[2:])
        if argv[0] == "pyright":
            return len(argv) > 1 and safe_paths(argv[1:])
        return False
    if check.project_type in {"node", "typescript"}:
        if argv[0] in {"npm", "pnpm", "yarn"}:
            if len(argv) == 3 and argv[1] == "run":
                return argv[2] in {
                    "test", "lint", "build", "typecheck", "type-check", "check-types",
                }
            return (
                len(argv) >= 5 and argv[1:3] == ("run", "test")
                and argv[3] == "--" and safe_paths(argv[4:])
            )
        if argv[:2] == ("node", "--check"):
            return len(argv) > 2 and safe_paths(argv[2:])
        return (
            argv[0] == "node_modules/.bin/tsc"
            and argv[1:] == ("--noEmit",)
        )
    if check.project_type == "rust":
        return argv in {
            ("cargo", "test", "--workspace"),
            ("cargo", "check", "--workspace"),
            ("cargo", "build", "--workspace"),
            ("cargo", "clippy", "--workspace", "--all-targets", "--", "-D", "warnings"),
        }
    return False


def _is_missing_verifier(
    exit_code: int,
    stderr: str,
    argv: tuple[str, ...],
) -> bool:
    if exit_code not in {1, 126, 127}:
        return False
    executable = argv[0] if argv else ""
    if executable in {"python", "python3", "python3.14"} and re.fullmatch(
        rf"(?:/bin/sh: \d+: )?{re.escape(executable)}: "
        r"(?:not found|No such file or directory)",
        stderr.strip(),
        re.IGNORECASE,
    ):
        return True
    if len(argv) >= 3 and argv[1] == "-m":
        expected_module = argv[2].split(".", 1)[0]
        if expected_module in {"pytest", "build", "ruff", "mypy", "pyright"}:
            for line in stderr.splitlines():
                missing_module = re.fullmatch(
                    r"(?:[^:\n]+: )?No module named ['\"]?([A-Za-z0-9_.-]+)['\"]?",
                    line.strip(),
                )
                if (
                    missing_module
                    and missing_module.group(1).split(".", 1)[0] == expected_module
                ):
                    return True
    return False


def _repairability(
    summary: str,
    output: str,
    intent: VerificationIntent,
    *,
    truncated: bool,
) -> Repairability:
    if truncated:
        return Repairability.UNKNOWN
    text = f"{summary}\n{output}"
    if re.search(
        r"(?im)^(?:.*(?:pyproject\.toml|package\.json|Cargo\.toml).*)?"
        r"(?:TOMLDecodeError|ConfigurationError|invalid configuration|"
        r"failed to parse (?:configuration|pyproject\.toml|package\.json)|"
        r"malformed package\.json)\b",
        text,
    ):
        return Repairability.CONFIGURATION_REPAIR_CANDIDATE
    if intent in {
        VerificationIntent.TARGETED_TESTS,
        VerificationIntent.RELEVANT_TESTS,
        VerificationIntent.FULL_TEST_SUITE,
    } and re.search(
        r"(?im)^(?:FAIL:|FAILED\s+\S+|.*\bAssertionError\b|"
        r".*\b(?:assertEqual|assertTrue|assertFalse)\b)",
        text,
    ):
        return Repairability.CODE_REPAIR_CANDIDATE
    if intent == VerificationIntent.SYNTAX_CHECK and re.search(
        r"(?im)^(?:.*\b(?:SyntaxError|IndentationError)\b|"
        r"\*\*\* Error compiling ['\"].+)",
        text,
    ):
        return Repairability.CODE_REPAIR_CANDIDATE
    if intent == VerificationIntent.TYPE_CHECK and re.search(
        r"(?im)^(?:\S+\.py:\d+(?::\d+)?: error:|"
        r".+\s+\d+\s+\d+\s+-\s+error:)",
        text,
    ):
        return Repairability.CODE_REPAIR_CANDIDATE
    if re.search(
        r"(?im)^(?:.*\b(?:ConnectionRefusedError|"
        r"Temporary failure in name resolution|"
        r"Name or service not known|Connection timed out)\b)",
        text,
    ):
        return Repairability.NOT_REPAIRABLE
    return Repairability.UNKNOWN


def _failure_excerpt(output: str, max_diagnostics: int) -> str:
    lines = output.splitlines()
    selected: list[str] = []
    for index, line in enumerate(lines):
        if _FAILURE_HINT.search(line):
            start = max(0, index - 1)
            for item in lines[start:min(len(lines), index + 3)]:
                if item not in selected and len(selected) < max_diagnostics:
                    selected.append(item)
            if len(selected) >= max_diagnostics:
                break
    excerpt = "\n".join(selected) if selected else "\n".join(lines[-min(max_diagnostics, 8):])
    return excerpt[-4096:].strip() or "Verification command exited nonzero."


def _bound_utf8(value: str, maximum: int) -> tuple[str, bool]:
    encoded = value.encode("utf-8", errors="replace")
    if len(encoded) <= maximum:
        return value, False
    if maximum <= 32:
        clipped = encoded[:maximum]
    else:
        marker = b"\n...[truncated]...\n"
        half = (maximum - len(marker)) // 2
        clipped = encoded[:half] + marker + encoded[-half:]
    return clipped.decode("utf-8", errors="ignore"), True


def _duration(value: object) -> float:
    if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0:
        return min(float(value), 86_400)
    return 0.0


def _is_read_only_without_verification(task: AgentTask) -> bool:
    if task.plan is None:
        return False
    read_only = {PlanOperation.READ, PlanOperation.SEARCH}
    return (
        all(step.operations and set(step.operations) <= read_only for step in task.plan.steps)
        and all(
            execution.status != ExecutionStatus.SUCCEEDED
            or execution.operation not in _MUTATIONS
            for execution in task.executions
        )
    )
