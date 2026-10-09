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
authorization. At validation, legacy `TEST` operations become the typed
`targeted_tests` verification intent. `VERIFY` is normalized only when a
typed verification intent is also present; an untyped `VERIFY` is rejected.
Neither remains a Phase 5 execution operation. Persisted plans that still
contain either operation are rejected by Phase 5 preflight before execution.
A DELETE is surfaced as destructive. Paths are normalized,
workspace-relative, checked component-by-component for symlinks, and checked
against the active root; new targets are allowed only for CREATE steps.
Verification intent contains fixed categories, never executable shell
commands. No planner result grants approvals or changes tool, sandbox,
network, or workspace policies.

Planned paths define permitted scope, not a requirement to modify every
listed file. Steps declare `required_outputs` when a particular
workspace-relative artifact must be produced; legacy plans without the
additive field remain readable.

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
in Phase 5; normalized `TEST`/`VERIFY` requirements are carried to Phase 6
rather than executed as Phase 5 tool operations.

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

A modifying step requires at least one successful in-scope mutation; its
other declared paths remain optional. Explicit `required_outputs` are checked
separately for a successful authorized mutation and current existence.
Runtime event callbacks are observational: ordinary callback failures are
logged and isolated from task transitions. Approval, cancellation, and
checkpoint persistence remain required control/state boundaries.

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

Process outcome, diagnostic completeness, and repairability are separate.
Known exit codes remain authoritative when output is truncated, but truncated
or ambiguous failures do not authorize automatic source repair. Repair
eligibility requires bounded evidence for a recognized assertion, syntax, or
type-check failure (or an explicitly classified configuration failure).
Missing verifier executables, approval denial, unavailable backends, and
external-service failures are not code-repair candidates; unrecognized
failures remain `UNKNOWN`. Missing-verifier detection uses precise
launch/module evidence rather than generic phrases in test output.

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
Repair entry is accepted only for a task in `REPAIRING` with a `CODE_FAILURE`;
unknown, blocked, and non-repairable verification outcomes do not authorize
automatic mutation.

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

Repair snapshots use the bounded Phase 2 source reader, which opens files
relative to the validated workspace with no-follow traversal, regular-file
validation, and size limits. Digests are confirmed by a second fresh bounded
read; missing, changed, unsafe, or unindexed targets fail capture rather than
expanding repair scope.

`RepairAttempt` records the triggering verification run/check, model
provenance, diagnosis, plan step, intended/mutated paths, tool execution IDs,
the follow-up verification run, repeated-failure/no-progress flags, and
bounded timestamps/errors. Checkpoints include pending tool executions before
dispatch. Recovery marks pending repair work interrupted and uncertain
executions interrupted; neither the mutation nor verification is replayed
automatically. Events expose repair lifecycle boundaries without exposing
hidden reasoning.

## Read-only review engine (Phase 8)

`CodingAgentRuntime.run_review` is a separate, explicit Phase 8 operation. It
accepts only a task in `REVIEWING` with completed plan steps and a successful,
current Phase 6 run. The required check results must match that verification
plan/run, all implementation calls must be settled, and the selected provider,
model, workspace, and execution backend must still match the task provenance.
The Phase 6 check requirements are fingerprinted before execution and compared
again before review so changing a required check to optional cannot silently
weaken the gate.
Before model review, the engine compares workspace-relative SHA-256
fingerprints for every recorded mutation with fingerprints captured when
Phase 6 completed. Stale or unavailable source, incomplete fingerprint
coverage, pending executions, context integrity errors, and cancellation
prevent completion; a new verification run is required after stale changes.
Selected Phase 3 source excerpts are refreshed using the current Phase 2
reader. Historical text is labeled historical-only in the prompt; stale
excerpts are not current evidence, and source quotes must match the refreshed
excerpt and its fingerprint.

The review request is bounded and reuses the Phase 3 `ContextEngine`. It
prioritizes the original goal, validated plan, actual changed source and tests,
repair history, required verification results, and selected conventions. Phase
3 inclusion reasons, confidence, resolution, limitations, and truncation data
are retained. Successful Phase 5/7 mutation records provide touched paths and
summaries. Since earlier phases did not retain pre-change snapshots or complete
patch previews, the reviewer explicitly receives that limitation and does not
claim to inspect a complete diff. Changed paths that cannot be safely
fingerprinted or gathered as indexed source block review instead of being
silently omitted.

The model receives a dedicated review prompt and an empty tool list. Any
provider-generated tool call is rejected; review never dispatches filesystem,
terminal, Git, or other mutation tools. Structured JSON is strictly validated
against the centralized finding categories, severity, confidence, paths,
locations, evidence quotes, and plan/execution references. A small configured
retry bound applies only to malformed review output. Blocking status is
derived by application policy: a high/critical, high-confidence finding must
include a quote and line location verified against current fingerprinted
source. An execution ID must match the cited path, operation, and step, but
execution summaries alone do not confirm a blocking source allegation. The
model's `suggested_blocking` preference cannot force or suppress the
application-derived value; persisted review records retain the compatible
`blocking` field. Categories cover correctness, regression risk, security, plan
alignment, test integrity, architecture consistency, maintainability, and
insufficient evidence.

Passing review (`PASSED` or `PASSED_WITH_WARNINGS`) persists a bounded review
record and transitions `REVIEWING → COMPLETED`. Material supported findings
produce `CHANGES_REQUESTED` and preserve the task in `REVIEWING`; blocked,
error, and cancelled reviews are persisted as non-completed outcomes when
checkpointing is available. No review finding is converted into a Phase 6
failure or automatically sent through Phase 7. Remediation must follow a
separately authorized and verified workflow; the reviewer itself never edits
source. Review limits bound prompt context, files/snippets, findings,
evidence, response size, retry attempts, duration, and persisted data.
Interrupted review recovery preserves earlier implementation and verification
evidence and never resumes a model call or mutation automatically.
Runtime, verification, repair, and review event callbacks are observational;
ordinary callback exceptions are logged without reversing persisted task
state. Cancellation remains authoritative, while approval and checkpoint
failures remain task/control failures.

## Git inspection, task changes, and safe checkpoints (Phase 9)

Git inspection is an optional evidence layer routed through the existing
`Tools` dispatcher and execution backend. Coding Agent Tasks may inspect
`git_status`, `git_diff`, `git_log`, and `git_show`; each operation requires
the existing terminal approval. Commands are application-owned, bounded,
noninteractive templates. Revisions are resolved from validated hexadecimal
commit IDs, paths are workspace-relative, and Git metadata/repository roots
outside the selected workspace are reported as unsupported rather than
expanding workspace authority. The selected host workspace and the backend's
execution-visible workspace are separate validated identities; sandbox Git
paths are interpreted under the fixed `/workspace` mount and exposed only as
workspace-relative paths. Git results identify complete, partial, failed,
timed-out, cancelled, and unavailable inspection outcomes; incomplete status
is never authoritative evidence of a clean repository. Machine-readable Git
output retains filename bytes through a bounded lossless transport. Git is not
required for task-specific change
tracking. Normal `Agent.turn` schemas remain unchanged; the Phase 9 schemas
are enabled explicitly.

Immediately before an approved Phase 5 or Phase 7 mutation is dispatched, the
runtime captures the first preimage for that task/path through the existing
no-follow, identity-checked `RepositoryIndex.read_file_bytes` reader. A
bounded private content-addressed snapshot stores source bytes; task state
stores only hashes, references, execution/repair attribution, outcome,
uncertainty, and bounded diff evidence. Later mutations retain the initial
task baseline and record their own before/after state. Failed, no-op,
interrupted, or externally discontinuous operations are not represented as
confirmed successful changes. Phase 6 source fingerprints remain the
authoritative freshness gate. Phase 8 review receives actual task-specific
before/after evidence when available and explicit limitations otherwise.

`git_checkpoint` creates an explicitly approved private snapshot and never
creates a Git commit or changes the index. `restore_checkpoint` validates the
checkpoint owner, workspace/backend identity, integrity, selected paths, and
all current file hashes before the first write. It restores the captured
workspace state, including pre-existing dirty content, using the existing
approved mutation backend; it does not run Git reset, checkout, clean, or
stash. Checkpoints are workspace/backend-bound snapshots independent of Git.
Optional repository identity is integrity-protected provenance, not restoration
authority or a precondition: restoration changes captured workspace files rather
than Git state and therefore does not require a live repository identity check.
The public checkpoint tool does not launch Git commands; when no identity is
supplied it explicitly reports that the checkpoint is Git-independent. The
snapshot is bounded to 64 paths, 1 MiB per file, 16 MiB total, 128
retained checkpoint records, and a 10-second capture duration; snapshot
content shares the private 64 MiB/1,024-object store. Restore preflight is
all-files-first and runs off the application event loop. Every selected target
and checkpoint is revalidated after approval; an external change causes a
conflict instead of being overwritten or reported unchanged. Multiple backend
mutations are not atomic and their per-file outcomes are reported. The current
text mutation backend cannot restore binary or
non-UTF-8 snapshots; such requests are rejected explicitly. Retention cleanup
is explicit, and interrupted/uncertain mutations are never replayed
automatically.

## Autonomy policy (Phase 10)

`synai.coding_agent.policies.AutonomyPolicy` is the deterministic policy
authority used by `CodingAgentRuntime` and `Tools.call`. It classifies exact
registered tool names using application-owned definitions; model-supplied
operation labels are ignored. A typed decision is `ALLOW`,
`REQUIRE_APPROVAL`, or `DENY`, with a stable reason code, explanation,
effective mode, category, source, constraints, and configuration fingerprint.
`inspect_policy` produces a deterministic human-readable explanation without
calling a model.

The precedence is hard security restrictions, backend/workspace identity,
validated arguments, operation classification, plan scope, active task/step,
cancellation, resource availability, policy fingerprint, permitted modes,
explicit denies, mandatory approval, then built-in safe-read rules and
explicit eligible exemptions. Unknown operations and evaluation failures are
denied. Policy evaluation never executes a tool and never replaces dispatcher,
workspace, backend, command, preimage, checkpoint, or restoration checks.

The built-in policy permits the existing bounded repository/file reads and
repository-intelligence calls, while file changes/deletion, terminal and
verification commands, network calls, Git inspection, checkpoint creation,
and checkpoint restoration continue to require the existing operation-level
approval. Plan approval remains a distinct SUPERVISED gate; it cannot approve
tool actions. AGENT remains the default task mode. AUTONOMOUS does not broaden
the built-in sensitive-operation policy.

Trusted per-user preferences may contain an additive `autonomy_policy`
object. Its version-1 typed schema supports a default mode, a permitted-mode
set, exact tool/category deny rules, and an exact allowlist of eligible
repository-intelligence tools. The only exemption supported is a sandbox-only
AUTONOMOUS policy `ALLOW` for one of those bounded intelligence tools; it does
not suppress a mandatory tool approval. Conflicting, wildcard, unknown, or
unsupported configuration is rejected. Existing settings without the
`autonomy_policy` field load with the secure built-in defaults. Workspace
files, model output, project context, and plan content are never policy
configuration sources. The application supplies the saved per-user policy to
the existing `Tools` dispatcher; headless callers may supply the same typed
configuration. No settings UI or new execution route is introduced in this
phase.

An active task stores its selected mode, policy version/fingerprint, backend
identity, and a hashed workspace identity. Pending tool dispatches bind the
exact tool/argument fingerprint and recheck task, plan scope, workspace,
backend, cancellation, and policy state after approval and before execution.
Any policy change invalidates a request tied to the previous fingerprint;
the operation is denied rather than inheriting newly expanded authority.
Checkpoint restoration continues to require explicit approval and its
post-approval all-target integrity/conflict preflight. Task policy audit rows
are bounded and contain only tool/category, decision/reason, mode, policy and
workspace fingerprints, approval outcome, backend, and execution references;
they do not contain tool arguments or file contents.

Policy metadata is additive in Agent Task state. Checkpoints without the new
fields remain readable and are treated conservatively as SUPERVISED when
authorization context is needed; no legacy task becomes AUTONOMOUS.

## Deterministic model routing (Phase 11)

`synai.coding_agent.routing.ModelRouter` selects models for PLANNING,
IMPLEMENTATION, REPAIR, and REVIEW using only the active `ModelProvider`.
The optional `Preferences.model_routing` object is private, trusted
application configuration; repository files, plan content, model responses,
and project memory cannot change it. Its version-1 schema strictly bounds
model profiles and per-role preferred/fallback lists. Model names are exact,
wildcards and duplicate profiles are rejected, and models must be present in
the currently configured provider inventory. Nothing pulls or downloads a
model.

The default `SINGLE_MODEL` mode preserves the conversation-selected model and
does not substitute another model. Explicit `ROUTED` mode supports
`PINNED`, `BALANCED`, and `CAPABILITY_FIRST` deterministic ranking. The
application selects each role model only when that stage is ready. A
candidate must advertise conversational generation; implementation and
repair also require native tools where the plan exposes them, while planner
and review requests use empty tool lists. Ollama's advertised `completion`
capability establishes conversational eligibility, and an explicitly
embedding-only model is rejected. Missing capability metadata remains
unknown, not verified. The legacy `SINGLE_MODEL` mode preserves the exact
selected model when chat metadata is unknown, but does not record chat as a
validated capability; known unsupported chat or required native-tool support
still fails closed.

An individual unavailable model or invalid/inaccessible candidate capability
record is isolated and recorded as a bounded candidate limitation. Global
inventory errors, discovery deadline exhaustion, cancellation, and provider
outages remain typed failures. The router does not inspect configured
fallbacks after a usable preferred pool has been established; `PINNED`
selection stops at the first eligible configured candidate. Fallback is only
from explicitly configured per-role fallback candidates and only before a
stage assignment has been committed. A missing/incompatible pool, unavailable
provider, endpoint change, or malformed configuration returns a typed routing
failure. There is no mid-stage model replacement.

Profiles contain only user-declared enabled state, eligible roles, relative
capability/resource/latency tiers, optional context capacity, and bounded
priority. These are preferences, not benchmark claims. Complexity estimates
use bounded task/context/plan/repair evidence and cannot change plan scope,
tool policy, approvals, verification, autonomy, or repair limits. Since
character budgets are not token counts, declared context capacity is recorded
as unverified and never proves prompt fit.

Each task checkpoint stores a configuration fingerprint, provider class,
endpoint fingerprint, immutable role/stage decisions, required and validated
capabilities, complexity reason, selection reason, candidate count, and
fallback outcome, candidate limitations, and bounded stage-selection events,
bound to the conversation session. Model inventory and capability discovery
share a bounded deadline. Before planning, implementation, repair, and review
begin, the locked model's installed state and mandatory capabilities are
revalidated under a fresh bounded discovery deadline. Per-step routed execution
checks retain the lock/provenance but do not repeat provider discovery. A
failed revalidation stops the stage; the committed assignment is not silently
replaced. It does not store inventories, prompts, source, or credentials.
Execution records identify the model that generated each tool request; repair
attempts and review records retain their actual model.
`AgentTask.selected_model` remains the conversation-selected model, and the
validated plan continues to identify its actual planner. Routed execution
validates the separate implementation assignment rather than equating these
models. Phase 6 remains deterministic and checks the plan, implementation,
repair, provider, endpoint, and source provenance without asking a verifier
model.

An interrupted task with no routing metadata remains a legacy single-model
task. History loading does not infer stage assignments or resume model
requests. Route changes while an approval is pending fail the dispatch guard;
provider failure after a stage begins preserves execution and pending-mutation
records and never causes an automatic retry through another model. The
existing `Tools` dispatcher and Phase 10 autonomy policy remain the sole
authorities for tool exposure and execution. A routing-selection event is sent
only after the stage assignment checkpoint succeeds. It contains bounded task,
stage, role, model, strategy, reason, and fallback metadata; observer failures
are logged and do not change persisted task state.

## Persistent project memory (Phase 12)

Project memory is an opt-in coding-agent feature. `Preferences.project_memory`
is trusted application configuration; legacy settings that omit it default to
disabled, including automatic capture. There are no memory model tools and no
normal-chat memory injection. Headless callers can use
`ProjectMemoryStore` for bounded list/search/get/add/update/archive/delete
operations; writes to user notes require an explicit user-originated
authorization argument.

The project namespace is derived from the canonical, existing validated
workspace directory, its device/inode, and the local user ID, then represented
by a SHA-256 identifier. The absolute path is not stored in the database.
Moving or replacing a workspace deliberately yields a different namespace;
automatic merging/reassociation is not supported. Host paths and a sandbox's
generic mount name are never used as aliases. Each store operation derives the
namespace from the validated workspace argument rather than accepting a
model-supplied project identifier.

Records are typed, bounded, and retain category, concise text, confidence,
status, task/conversation/verification/review provenance, relative evidence
paths and fingerprints, and timestamps. Automatic extraction creates only a
`VERIFIED_OUTCOME`: Phase 8 must have checkpointed a completed task, required
Phase 6 checks must pass, review must pass (warnings allowed), and complete
certain Phase 9 task-attributed postimages must still match Phase 2 safe reads.
Repair attribution is included only when a repair succeeded and reverification
passed. Read-only, failed, cancelled, uncertain, approval-denied, or
unreviewed work is not learned automatically. Captures are idempotent by
task/evidence key; user-pinned notes are not replaced by automatic outcomes.
Deleting an automatic capture also retains a bounded tombstone so retrying an
old checkpoint cannot silently recreate it.

The private store is SQLite at
`<application storage>/project-memory/memory.sqlite3`, outside source
repositories and conversation checkpoints. Database schema version 2 adds a
transactionally maintained normalized-token/path/symbol candidate index;
existing Phase 12 schema-version-1 databases are transactionally indexed
without changing record payloads or project ownership. Unknown future or
corrupt databases fail explicitly and are never reset. Parent directories and
the database are owner-only, database access uses parameterized statements
and bounded transactions, and SQLite busy handling serializes concurrent
writers. Record count, byte size, content, evidence-path, retrieval, and
tombstone limits are validated. At capacity, writes fail explicitly rather
than evicting pinned memories or task history.

Retrieval first requires an exact workspace-relative evidence path/filename,
exact evidence symbol, or normalized meaningful-token overlap. Verified
provenance, user-pinned status, and source-evidence quality only affect ranking
after this eligibility gate. Indexed candidate discovery is project- and
active-status-scoped across the full supported namespace (up to 4,096 records),
then deterministic relevance ranking uses specificity, provenance, freshness,
recency, and memory ID tie-breaking. Query work is bounded by the validated
query shape (64 distinct normalized terms; oversized queries fail explicitly),
indexed candidate ceiling, configured deadline, cancellation, result count,
character budget, and selected-source byte budget. No arbitrary recent-record
window can hide an older indexed match. There are no Ollama,
embedding, vector, or external-search calls; lexical matching may miss
semantic paraphrases. Empty eligible candidate sets remain empty.

Selected source-linked memories are checked against current workspace-relative
fingerprints through `RepositoryIndex.read_file_bytes`, limited to selected
paths and a bounded total byte budget; this avoids a repository-wide rescan.
Changed source marks an active record stale and excludes it; missing or unsafe
evidence is unavailable and excluded. An explicitly authorized correction
may promote an active or stale record to an active user-confirmed note,
preserving its historical task identifiers while removing obsolete source
fingerprint claims. Archived, superseded, and deleted records cannot be
revived through correction. Unlinked user-pinned/corrected notes remain
explicitly unverified. Manual text search returns active indexed matches but
has no repository reader argument and therefore does not independently
fingerprint source; it is separate from task-context retrieval.

Manual replacement and automatic freshness/status updates use conditional
record-JSON compare-and-swap writes. Freshness reads happen before their
single-statement conditional write; a concurrent edit, archive, supersession,
or deletion makes the compare fail, so the old snapshot is not persisted or
returned as current context. Manual update/archive/delete operations and
automatic capture/index changes are transactional; every store operation
opens its own SQLite connection rather than sharing one across threads.

Relevant records enter only the existing Phase 3 `ContextPackage` as
`ContextKind.MEMORY`, after repository evidence has been selected. Memory can
consume at most 20% of the usable character budget and never consumes the
planner reserve; truncation and store/evidence limitations are surfaced.
Prompt content is separately labeled untrusted historical/user data, not
current source or authorization. Current source, plan validation, Phase 5
scope and approvals, Phase 6 verification, Phase 8 review, Phase 10 policy,
and Phase 11 routing remain authoritative. Capture runs only after review has
persisted task completion; a memory-write error emits a separate failure
event and does not roll back the task, replay mutations, or rerun checks.
