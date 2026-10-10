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

Project activity is currently read from persisted project, snapshot, and task
APIs; the WebSocket stream is conversation-scoped. Live project-activity event
delivery, Agent Task execution, Client Agent transport, Sandbox Broker
execution, verification/review/diff panels, and client-side patch application
remain future work.
