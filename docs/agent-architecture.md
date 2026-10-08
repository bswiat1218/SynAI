# SynAI architecture baseline

This document records the current architecture before adding the opt-in
coding-agent workflow. Existing components remain the authority for their
respective responsibilities; the Phase 1 state model is additive.

## Chat and streaming

`CodingApp.action_send` validates that a conversation, model, workspace, and
execution environment are available, then starts `CodingApp.run_turn`.
`Agent.turn(session, model, prompt)` appends the user message and the persistent
system instruction, saves history, and calls the configured `ModelProvider`.
The Ollama provider streams `ChatEvent` values containing text, provider
reasoning, native calls, and the completion marker. The agent updates the
assistant message and UI during the stream and periodically saves partial
output. A response with no tool calls ends the turn; normal chat does not
require agent planning, repository indexing, or verification.

The current execution-environment instruction is built into a separate copy
of the conversation messages for each provider request. It is not appended to
`Session.messages` and is not persisted. The persistent `SYSTEM` instruction
is stored once in conversation history.

## Agent and tool flow

The existing `synai.agent.Agent` is the normal chat/tool loop, not yet the new
task state machine. When a model advertises native tools and the configured
execution backend matches the selected conversation, the agent sends the
existing tool schemas. It validates the returned native-call shape, accounts
for the per-turn tool budget, records request/result activity, executes calls
through `synai.tools.Tools`, appends tool results to conversation history, and
returns them to the model.

`Tools.call` checks tool names, exact argument keys/types, and argument size.
Writes, patches, and deletes are previewed and diffed; the backend verifies the
approved content hash before mutation. Every operation requiring approval
awaits the application approval callback. The agent does not access the
filesystem or spawn commands directly.

Phase 2 adds ten read-only repository-intelligence tools to this same registry.
They do not accept filesystem paths, do not call `ExecutionBackend.execute`,
and do not request mutation approvals. `Tools` obtains the active backend's
workspace, validates it using the existing workspace validator, and passes it
to a bounded in-process index. The regular agent's backend-match gate, native
tool budget, cancellation, and history flow still apply.

## Approval flow

`CodingApp.approve` presents one action at a time using the approval modal,
records the request and decision in conversation activity, and resolves the
waiting callback. A denied operation is returned as a denied tool result; it
does not reach backend execution. Tool-budget extension also requires an
explicit approval. Phase 1 adds no autonomy mode and changes no approval
requirements.

## Workspace and sandbox flow

The conversation environment is validated and stored with the conversation.
Sandbox workspaces are private managed conversation directories. The container
is checked for non-root execution, no-new-privileges, dropped capabilities,
read-only root filesystem, a single selected workspace bind mount, and
resource limits. The helper rejects absolute/traversal/symlink paths and
bounds command duration and output.

Host tools are a separately selected Linux execution mode, refuse root, run
through the isolated helper process with a workspace boundary and
no-new-privileges, and are clearly identified as not sandboxed. Workspace
matching, backend validation, and per-operation approvals remain mandatory.

## Repository intelligence (Phase 2)

`synai.intelligence.RepositoryIndex` is an in-memory, workspace-scoped Python
AST index. Every query rescans the selected workspace using sorted relative
paths, fingerprints eligible UTF-8 text files, and reparses only files whose
content fingerprint changed. Changed files replace their records; deleted or
unobserved files are not carried forward. No repository state is persisted.
Traversal uses directory file descriptors with no-follow opens, skips symlink
entries and directory symlinks, and cannot be directed to another path by
tool arguments.

The centrally defined exclusions cover VCS metadata, virtual environments,
dependency directories, Python/tool caches, coverage output, and common build
directories. Defaults cap indexing at 10,000 text files, 20,000 entries per
directory, 1 MiB per file, 64 MiB total source, depth 32, and 10 seconds per
scan. Query results are capped at 100 search results or 500 symbol/reference
records; diagnostics at 200, snippets at 500 characters, and serialized
intelligence output at 512 KiB (or the configured tool-output limit if
smaller). Limits are validated and tests can inject smaller values. Hitting a
limit produces structured truncation diagnostics rather than an unbounded
scan.

Python modules, classes, functions, async functions, methods, imports, lexical
references, call expressions, and uniquely-resolvable direct class bases are
indexed with workspace-relative locations. Import resolution records source
syntax, including simple relative imports and aliases. References and callers
are explicitly lexical/syntactic rather than binding-aware; dynamic dispatch,
runtime aliases, and ambiguous inheritance are not guessed. Test discovery
uses `tests/`, `test_*.py`, `*_test.py`, and `test_`/`Test` symbol conventions;
it does not claim test coverage. Search reads only allowlisted text/code
extensions and skips NUL-containing or invalid-UTF-8 content. Diagnostics
include Python AST syntax errors, read failures, and scan-limit state; no
external analyzer or test command is run.

The exposed operations are `get_project_structure`, `find_symbol`,
`find_definition`, `find_references`, `find_callers`,
`find_implementations`, `find_imports`, `find_tests`, `search_code`, and
`get_diagnostics`. All use strict JSON schemas and return bounded structured
results. This subsystem provides no prompt context selection, planning,
editing, verification, or project memory.

## Context engine (Phase 3)

`synai.coding_agent.ContextEngine` accepts a task string plus the validated
Phase 2 `RepositoryIndex`, optional task/step metadata, workspace-relative
previous modifications/selections, a character budget, and a bounded query
result limit. It extracts explicit paths and filenames, dotted/quoted names,
identifiers, and lexical terms without model inference. Explicit files and
qualified symbols receive higher fixed scores than identifiers and broader
literal matches. Centralized weights, reason codes, candidate ordering, and
tie-breaking live in `coding_agent/context.py`.

The engine reuses only `RepositoryIndex.query`, `read_source`, and
`read_symbol_source`; it contains no scanner or parser. Source extraction
keeps bounded definition ranges or small line-centered evidence ranges. The
character cost is `len(content)` (Unicode code points), separate from rendered
labels; a configurable fraction of the character budget remains reserved for
future planner instructions. Candidate caps, source line/character caps,
repository query count, and a context-build deadline bound work and output.
Candidates that do not fit are omitted in score order and exposed as
expansion candidates. Context packages are typed, serializable values; they
are not added to conversation history or normal chat requests.

Every selected item carries machine-readable reasons, source, confidence, and
resolution. Exact AST definitions and explicit paths are high confidence;
references and callers remain low confidence with the Phase 2 lexical or
syntactic limitation attached. Related tests are chosen by file basename,
test symbol names, explicit test names, or lexical test references. Imports
are expanded only one level and only where Phase 2 resolves a definition.
Diagnostics are limited to selected/named files, plus global index-truncation
limitations. `render_context` renders bounded explanatory text but is not
connected to `Agent.turn` in this phase. `expand_context` accepts an explicit
bounded target and reuses the same index and workspace-safe source reader.
Phase 3 has no plan execution, verification, code repair, review, Git
integration, model routing, memory, or task UI.

## Planning engine (Phase 4)

`synai.coding_agent.Planner` accepts a task, typed Phase 3 context package,
typed operation capabilities, the already-validated active workspace, and
the model selected by the conversation. It uses the existing
`ModelProvider.chat(...)` stream with no tools; it does not create another
provider client or call Ollama directly. Planner prompts preserve context
selection reasons, confidence, resolution, limitations, truncation and budget
metadata, and label source context as incomplete/untrusted evidence.

Model output is untrusted strict JSON. Unknown or missing fields, duplicate
JSON keys, oversized data, malformed step IDs, invalid operations, invalid
workspace-relative paths, unavailable capabilities, unresolved dependencies,
cycles, missing modification targets, and absent verification intent fail
deterministic application validation. Dependencies are topologically ordered
stably while preserving the model order where possible. Symbol and context
mismatches are warnings rather than proof of invalidity because Phase 3 context
is deliberately selective. Lexical and syntactic evidence limitations remain
warnings.

Plan operations (`read`, `search`, `create`, `modify`, `delete`, `test`,
`verify`, `document`) and verification intents are typed categories, not
authorization. A DELETE is surfaced as destructive. Paths are normalized,
workspace-relative, checked component-by-component for symlinks, and checked
against the active root; new targets are allowed only for CREATE steps.
Verification intent contains fixed categories, never executable shell
commands. No planner result grants approvals or changes tool, sandbox,
network, or workspace policies.

Malformed or invalid model output receives at most one bounded correction
request by default, including structured validation errors. Provider failures,
cancellation, and exhausted attempts return typed results with no plan. A
successful `AgentPlan` contains steps, purposes, prerequisites, targets,
operation intent, outcomes, verification criteria, assumptions, uncertainties,
completion criteria, stable executable order, context hash/truncation state,
validation warnings, and provider/model metadata. The plan can be attached to a
Phase 1 task only while it is in `PLANNING`; its checkpoint remains a separate
machine-state record. Legacy Phase 1 plan/step payloads remain readable.

`render_plan` is a bounded deterministic presentation helper. Phase 4 stops
after plan validation/attachment: it does not execute steps, dispatch tools,
run tests, approve a plan, change task autonomy, or transition to
`IMPLEMENTING`. Normal `Agent.turn(...)` and the conversation transcript are
unchanged.

## Coding-agent execution runtime (Phase 5)

`CodingAgentRuntime.run_task(...)` is the opt-in headless workflow. It keeps
normal `Agent.turn(...)` independent, gathers Phase 3 context, invokes the
Phase 4 planner (or accepts an already planner-produced plan), presents the
bounded rendered plan, applies the selected plan-approval mode, and executes
steps in the plan's validated dependency order. The task lifecycle is now
`UNDERSTANDING → CONTEXT_GATHERING → PLANNING → IMPLEMENTING → VERIFYING`.
The serialized state names and checkpoint versions are unchanged. Read-only
tasks with no verification intent may end `COMPLETED`; a modifying task ends
in `VERIFYING`, meaning implementation steps are ready for Phase 6, not that
the code has been verified.

`SUPERVISED` requires a separate explicit plan decision. Denial runs no steps.
`AGENT` and `AUTONOMOUS` begin after plan validation; `AUTONOMOUS` does not
expand tool authority. Every tool call still goes through the existing
`Tools.call(...)`, its schema checks, approvals, previews/hash checks, and
`ExecutionBackend`. A step policy exposes only read/intelligence and
operation-compatible file tools. Terminal and network tools are not exposed
in Phase 5; `TEST`/`VERIFY` plan steps stop explicitly at the Phase 6
boundary.

The runtime revalidates model availability, backend/session/workspace identity,
the provider/model provenance recorded by the plan, plan schema and order,
paths, symlink boundaries, step dependencies, operation categories, and
tool/task resource limits before execution and again before each step.
Preflight follows executable order when a later step targets a path declared
for an earlier create; each step still checks the actual current filesystem.
Read-only file exploration may use any safely bounded workspace path. Mutation
tools must target a path declared by the active plan step and must match its
create/modify/delete intent; an undeclared target is rejected before approval
or dispatch. A discovered additional mutation target requires a new plan
rather than implicit scope expansion.

Each step receives only matching Phase 3 task/path/symbol context, bounded
step metadata, limitations, and concise summaries of prior successful tool
calls. The bounded native-tool loop records executions in the Phase 1
checkpoint. A modifying call is checkpointed as pending before dispatch and
resolved after the existing tool result. Recovery marks uncertain calls and
the active step interrupted and never replays them. Cancellation and failures
stop scheduling subsequent steps. The runtime does not run verification,
replanning, code repair, review, Git operations, model routing, or UI flows.

## Verification engine (Phase 6)

`CodingAgentRuntime.run_verification(...)` is an explicit Phase 6 entry point
for a Phase 5 task in `VERIFYING`. Normal chat and implementation remain
independent. The verifier revalidates task/provider provenance, completed plan
steps, successful mutation records, workspace and backend identity, paths,
timeouts, and output/resource limits before planning and before every check.

Project discovery is bounded and does not follow symlinks. It selects the
nearest manifest roots associated with declared/modified plan paths, then
maps only Phase 4's typed verification intents to application-owned commands.
Python checks use explicit configuration, dependencies, or test conventions;
Node checks use declared package scripts and package-manager evidence; Rust
checks use Cargo and only the requested intent. Missing evidence creates a
structured unavailable check instead of a guessed command. Targeted tests
must be declared in plan paths; relevant tests are selected deterministically
from bounded Phase 3/path evidence. The full suite runs only for an explicit
full-suite intent.

Every executable check is revalidated and dispatched through the registered
`terminal` tool. Existing terminal schema validation, approval callbacks,
backend execution boundaries, tool budgets, and cancellation remain
authoritative. Command arguments come from fixed application templates and
validated workspace-relative paths, not task/model prose. Per-check and
per-run output is bounded and persisted with typed status, exit code, timing,
approval state, and concise failure/infrastructure evidence.

A required command failure transitions `VERIFYING → REPAIRING` with
`CODE_FAILURE`; a successful verification transitions `VERIFYING → REVIEWING`
and returns `ready_for_review`. The verifier stops at either boundary: it
does not repair code, run a review model, or mark the task complete. Approval
denial, unavailable tooling, policy/backend errors, and timeouts remain
blocked/error outcomes and are not treated as source failures. A read-only
task that reaches verification without executable intent records
`NO_VERIFICATION_NEEDED`, not a fabricated pass.

The validated `VerificationLimits.fail_fast_on_syntax_failure` policy defaults
to enabled: after a syntax failure it continues independent linting but marks
tests, type checking, builds, and the full suite skipped with a reason. A
caller may disable that policy to run all planned checks.

Checks are checkpointed before dispatch and after completion. Recovery marks
an in-flight check `INTERRUPTED` and never replays it. A caller may explicitly
restart an interrupted verification through
`run_verification(..., restart_interrupted=True)`; a new run ID is created,
prior uncertain evidence is retained, and cumulative output/run limits still
apply. No restart occurs automatically.

## Lifecycle refinement

The allowed state transition table now reflects the actual pipeline in which
context precedes planning. Previously saved checkpoints retain their state
names and remain decodable; loading does not replay or infer new transitions.
Starting a new coding-agent task uses the refined ordering and transition
validation remains strict.

## Persistence

`ManagedHistory` stores each conversation as `conversation.json` in its
private managed folder. Writes use a temporary file and atomic replacement.
History schema versions 1–5 are accepted. The conversation transcript,
activity, environment snapshot, execution limits, and provenance are
serialized; the machine-facing task state is separate from human-facing
messages and activity.

Phase 1 introduces an optional, typed `agent_checkpoint` in schema version 6.
Records without a checkpoint retain the existing version/shape. Existing
versions 1–5 remain loadable, and checkpoint data is validated strictly.
Recovery marks active checkpointed tasks interrupted and marks uncertain
pending executions as interrupted; no operation is automatically replayed.

## Cancellation and recovery

Escape/STOP cancels the active `CodingApp` turn task. The agent retains partial
output, marks the assistant message cancelled, resolves each unmatched native
call with an explicit “not replayed” result, and records cancellation. Sandbox
and host helpers attempt bounded process cleanup. On reopening a history,
`Agent.recover` marks an interrupted running turn and resolves outstanding
calls without invoking tools.

The Phase 1 task state machine has explicit terminal states. A recovered active
task is marked `interrupted`; it is not resumed automatically. The normal chat
`Session.state` and its recovery behavior remain independent of the new
machine-facing checkpoint.

## Baseline

Before Phase 1 changes, the existing unit suite completed with Python 3.14.8:

```text
python -m unittest discover -s tests -v
205 tests passed
18 skipped (optional integration tests)
```

Container, graphical desktop, and editor-image tests are opt-in and were not
enabled for this baseline run.

## Bounded repair loop (Phase 7)

`CodingAgentRuntime.run_repair` is an explicit follow-on to a Phase 6
`CODE_FAILURE`. The repair controller accepts only required failed checks
whose Phase 6 result is classified as a code or configuration repair
candidate. It does not ask a model to respond to blocked, unavailable,
cancelled, interrupted, or operational verification results.

Each attempt revalidates the selected provider/model, task and plan, active
workspace/backend, plan paths, and cancellation state. It builds failure-
focused bounded context using the Phase 3 context engine and requests a
concise structured diagnosis from the already selected model. A diagnosis
cannot grant mutation authority: target paths must already belong to a
completed mutable Phase 4 plan step. Test and configuration paths remain
eligible only when the original task and validated plan authorize them.

Repair tool calls use the existing Phase 5 step executor, `Tools.call`,
approval callbacks, and configured execution backend. The repair-mode tool
set excludes deletion, terminal execution, and network access. Reads remain
workspace-bounded; every mutation is independently checked against the
validated plan and the diagnosed target subset. An out-of-scope requirement
returns `REPLAN_REQUIRED`; Phase 7 does not invoke the planner or widen scope.
Approval denial terminates the attempt without an alternative mutation.

The application enforces a default maximum of two repair attempts. Per-attempt
model rounds, tool calls, execution time, response size, tool-result size, and
failure/context excerpts are bounded by Phase 5 limits and repair limits (the
default repair tool-result feedback cap is 4 KiB). A
successful tool response is not considered a repair: at least one meaningful
in-scope content change is required, and every completed repair mutation is
followed by the existing Phase 6 verification plan. The model cannot choose
weaker verification. A passing verification leaves the task in `REVIEWING`;
Phase 7 does not review or complete the task.

`RepairAttempt` records the triggering verification run/check, model
provenance, diagnosis, plan step, intended/mutated paths, tool execution IDs,
the follow-up verification run, repeated-failure/no-progress flags, and
bounded timestamps/errors. Checkpoints include pending tool executions before
dispatch. Recovery marks pending repair work interrupted and uncertain
executions interrupted; neither the mutation nor verification is replayed
automatically. Events expose repair lifecycle boundaries without exposing
hidden reasoning.
