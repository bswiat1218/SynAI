"""Typed context, planning, and state APIs for opt-in coding-agent tasks."""

from synai.coding_agent.context import (
    ContextConfidence,
    ContextEngine,
    ContextExpansionRequest,
    ContextItem,
    ContextKind,
    ContextLimits,
    ContextPackage,
    ContextRequest,
    parse_task,
    render_context,
)
from synai.coding_agent.changes import ChangeBaseline, MutationEvidence, SnapshotStore
from synai.coding_agent.policies import (
    AutonomyMode,
    AutonomyPolicy,
    AutonomyPolicyConfig,
    OperationCategory,
    PolicyAuditRecord,
    PolicyDecision,
    PolicyDecisionType,
    PolicyReason,
    PolicySource,
    PolicyTaskContext,
    inspect_policy,
)
from synai.coding_agent.state import (
    AgentCheckpoint,
    AgentErrorType,
    AgentExecution,
    AgentPlan,
    AgentStatus,
    AgentStep,
    AgentTask,
    ApprovalStatus,
    ExecutionStatus,
    PlanOperation,
    RepairAttempt,
    RepairOutcome,
    Repairability,
    RepairStatus,
    ReviewCategory,
    ReviewConfidence,
    ReviewFinding,
    ReviewOutcome,
    ReviewRecord,
    ReviewSeverity,
    StepStatus,
    VerificationCheck,
    VerificationOutcome,
    VerificationPlan,
    VerificationResult,
    VerificationIntent,
    VerificationStatus,
)
from synai.coding_agent.planner import (
    Planner,
    PlannerLimits,
    PlanningErrorCode,
    PlanningIssue,
    PlanningRequest,
    PlanningResult,
    PlanningSeverity,
    PlanningWorkspace,
    attach_validated_plan,
    render_plan,
)
__all__ = [
    "ContextConfidence",
    "ContextEngine",
    "ContextExpansionRequest",
    "ContextItem",
    "ContextKind",
    "ContextLimits",
    "ContextPackage",
    "ContextRequest",
    "ChangeBaseline",
    "MutationEvidence",
    "SnapshotStore",
    "AutonomyPolicy",
    "AutonomyPolicyConfig",
    "OperationCategory",
    "PolicyAuditRecord",
    "PolicyDecision",
    "PolicyDecisionType",
    "PolicyReason",
    "PolicySource",
    "PolicyTaskContext",
    "inspect_policy",
    "AgentCheckpoint",
    "AgentErrorType",
    "AgentExecution",
    "AgentPlan",
    "AgentStatus",
    "AgentStep",
    "AgentTask",
    "ApprovalStatus",
    "AutonomyMode",
    "CodingAgentRunResult",
    "CodingAgentRuntime",
    "ExecutionStatus",
    "PlanOperation",
    "Planner",
    "PlannerLimits",
    "PlanningErrorCode",
    "PlanningIssue",
    "PlanningRequest",
    "PlanningResult",
    "PlanningSeverity",
    "PlanningWorkspace",
    "PlanApprovalDecision",
    "RepairAttempt",
    "RepairController",
    "RepairLimits",
    "RepairOutcome",
    "RepairRequest",
    "RepairRunResult",
    "Repairability",
    "RepairStatus",
    "ReviewCategory",
    "ReviewConfidence",
    "ReviewEngine",
    "ReviewFinding",
    "ReviewInput",
    "ReviewLimits",
    "ReviewOutcome",
    "ReviewRecord",
    "ReviewRequest",
    "ReviewRunResult",
    "ReviewSeverity",
    "RuntimeErrorCode",
    "RuntimeEvent",
    "RuntimeFailure",
    "RuntimeLimits",
    "StepStatus",
    "VerificationResult",
    "VerificationCheck",
    "VerificationOutcome",
    "VerificationPlan",
    "VerificationIntent",
    "VerificationStatus",
    "attach_validated_plan",
    "parse_task",
    "render_context",
    "render_plan",
]

__all__ += [
    "VerificationEngine",
    "VerificationLimits",
    "VerificationRequest",
    "VerificationRunResult",
]

def __getattr__(name: str):
    runtime_names = {
        "AutonomyMode",
        "CodingAgentRunResult",
        "CodingAgentRuntime",
        "PlanApprovalDecision",
        "RuntimeErrorCode",
        "RuntimeEvent",
        "RuntimeFailure",
        "RuntimeLimits",
    }
    if name in runtime_names:
        from synai.coding_agent import runtime

        return getattr(runtime, name)
    verification_names = {
        "VerificationEngine",
        "VerificationLimits",
        "VerificationRequest",
        "VerificationRunResult",
    }
    if name in verification_names:
        from synai.coding_agent import verifier

        return getattr(verifier, name)
    repair_names = {
        "RepairController",
        "RepairLimits",
        "RepairRequest",
        "RepairRunResult",
    }
    if name in repair_names:
        from synai.coding_agent import repair

        return getattr(repair, name)
    review_names = {
        "ReviewEngine",
        "ReviewInput",
        "ReviewLimits",
        "ReviewRequest",
        "ReviewRunResult",
    }
    if name in review_names:
        from synai.coding_agent import reviewer

        return getattr(reviewer, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
