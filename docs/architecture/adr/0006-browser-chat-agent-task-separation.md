# ADR 0006: Browser Chat is separate from Agent Tasks

- **Status:** Accepted and implemented for chat-only Browser Chat; distributed
  Agent Task execution remains disabled. Historical approval record is absent.
- **Date:** 2026-10-10 baseline documentation.

## Decision

Browser Chat invokes the existing conversation/provider flow with tool
execution explicitly disabled. Agent Tasks are a separate future workflow
requiring their own task state, snapshot, target, claim, policy, approval,
Broker result and recovery contracts. A browser chat project reference is
metadata only. Neither model-emitted tool calls nor browser authentication
can create an Agent Task or dispatch execution.

## Rationale

Chat identity and conversational input do not provide a validated plan,
scoped approval or task claim. Separation also preserves the legacy local
`CodingAgentRuntime` lifecycle and its authorization model.

## Alternatives considered

- Enable existing TUI tool schemas in browser chat: rejected.
- Treat a project chat as a task: rejected.
- Infer approval from session authentication: rejected.
- Present metadata pages as operational task or sandbox controls: rejected.

## Consequences

- Browser UI displays unavailable task/target status explicitly.
- API currently lists persisted disabled task records only; no task create or
  execute route exists.
- Future task start must be explicit and versioned, with independent policy,
  claim/fencing, approval and Broker enforcement.

## Implementation dependencies

Implemented in `synai/web/chat.py`, `app.py`, schemas and React UI.
Representative tests: `tests/test_web_chat.py`,
`tests/test_conversation_browser.py`, `tests/test_web_openapi.py` and
`web/e2e/`.

## Historical input

Phase 13D explicitly describes tool-disabled chat and disabled execution.
The original approved Phase 13A/realignment plan was unavailable.
