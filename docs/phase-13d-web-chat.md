# Phase 13D — Browser Chat and Command Center

Phase 13D adds an authenticated, chat-only browser application over the existing
provider and conversation engine. It does not add Agent Task execution, host or
sandbox tools, filesystem access, or patch application.

## Browser chat

The versioned `/api/v1/chat` API creates, lists, reads, and resumes persisted
conversations; starts turns; and explicitly cancels active turns. Sessions use
the existing managed conversation history and data-root ownership lock, with a
versioned metadata index for logical-project association and event cursors.
Interrupted turns are marked interrupted on recovery and are never replayed.

Chat constructs the existing `Agent` with tool execution explicitly disabled.
It creates no `Tools` dispatcher, sends no native tool schemas to the provider,
and rejects model-emitted tool calls without executing them. Model-emitted
reasoning is persisted separately from assistant-visible content.

The authenticated `/api/v1/events/v1/chat/{conversation_id}` WebSocket uses the
browser session cookie and validates the configured Origin. Its version-1 event
envelope has a monotonic per-conversation cursor. Persisted events support
replay and resynchronization; bounded queues request a state refresh on
overflow. Reconnection does not resubmit turns. The persisted conversation is
authoritative when event delivery is interrupted.

Project activity is available from the authenticated
`GET /api/v1/logical-projects/{project_id}/activity` endpoint and the
`/api/v1/events/v1/projects/{project_id}` WebSocket. Both re-check project
authorization; the socket also validates the exact configured Origin and
browser session. Version-1 events have a durable, monotonic per-project cursor.
The stream replays retained events, sends an explicit resynchronization marker
and project snapshot when a cursor is outside retained history, and closes when
the browser session expires or is revoked. Events are persisted transactionally
with supported project, workspace-binding, device-revocation, and snapshot
transitions. The Workbench refreshes authoritative APIs on updates or detected
gaps; WebSocket delivery is never authoritative state.

## Browser application

The React application provides the Command Center, general and project-scoped
Chat, Projects, a logical-project Workbench, Agent Task metadata, Devices,
Sandboxes, and Settings. It uses server API data and describes absent providers,
devices, and execution infrastructure as unavailable rather than simulating
them. Browser drafts are held in memory only and are preserved while navigating
between conversations.

The Workbench exposes project, snapshot, activity, and memory-status panels.
Project references are metadata only; they do not expose source workspaces.
Registered device activity is not represented as a live connection. The
Sandbox Broker and task execution remain unavailable.

## Limits and boundary

The service applies limits to prompt bytes, transcript bytes, output bytes,
message count, sessions, active turns, event size, retained events, subscribers,
and WebSocket input. Browser endpoints require the existing authenticated
session; state-changing endpoints also require CSRF validation. Credentials and
drafts are not stored in browser local storage.

Project activity covers persisted metadata and snapshot transitions only; it
does not imply task execution, Client Agent connectivity, approvals, or sandbox
activity. Agent Task execution, Client Agent transport, Sandbox Broker
execution, verification/review/diff panels, and client-side patch application
remain future work. A project reference in chat is metadata only and provides
no source-file access.

## Development and browser acceptance checks

Start the API with `synai-web` using an isolated `SYNAI_DATA_ROOT` equivalent
through an injected `WebConfig` in tests; never point integration tests at a
live TUI data root. For the frontend, run `cd web && npm install` followed by
`npm run dev`. Run component checks, static typing, and a production build with
`npm test`, `npm run typecheck`, and `npm run build`.

The Playwright acceptance suite is started with `cd web && npm run test:e2e`.
It uses an isolated Vite server and intercepts API and WebSocket traffic with
deterministic test fixtures; it does not call Ollama or require an application
database. Install a supported Playwright browser separately when permitted by
the host operator (`npx playwright install chromium`). If the browser runtime is
not present or its installation is not approved, report the browser suite as
unexecuted rather than passing. Backend persistence, authorization, and provider
behavior remain covered by the Python test suite using disposable data roots
and injected mock providers.

The browser application supports keyboard focus indicators, semantic page and
navigation landmarks, live response status, Radix-managed mobile navigation,
responsive desktop/tablet/mobile layouts, and reduced-motion preferences.
Manual acceptance should additionally cover keyboard-only operation, browser
zoom, narrow layouts, and assistive-technology behavior; automated component
and browser tests do not constitute a complete WCAG audit.
