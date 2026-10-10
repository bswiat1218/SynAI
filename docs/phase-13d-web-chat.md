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
between conversations. At Phase 13D completion, device activity was metadata
only; Phase 13E adds a real Linux Client Agent and authenticated live presence,
documented in [Phase 13E](phase-13e-client-agent.md).

The Workbench exposes project, snapshot, activity, and memory-status panels.
Project references are metadata only; they do not expose source workspaces.
At Phase 13D completion, registered device activity was not represented as a
live connection. Phase 13E adds actual Client Agent connection state. The
Sandbox Broker and task execution remain unavailable.

## Limits and boundary

The service applies limits to prompt bytes, transcript bytes, output bytes,
message count, sessions, active turns, event size, retained events, subscribers,
and WebSocket input. Browser endpoints require the existing authenticated
session; state-changing endpoints also require CSRF validation. Credentials and
drafts are not stored in browser local storage.

Project activity covers persisted metadata and snapshot transitions only; it
does not imply task execution, Client Agent connectivity, approvals, or sandbox
activity. At Phase 13D completion, Agent Task execution, Client Agent
transport, Sandbox Broker execution, verification/review/diff panels, and
client-side patch application remained future work. Phase 13E implements only
the Client Agent transport and consented source snapshots; task execution,
Broker execution, verification, and patch application remain disabled. A
project reference in chat is metadata only and provides no source-file access.

## Development and browser acceptance checks

Install locked frontend dependencies with `cd web && npm ci`. The UI-only
Playwright suite (`npm run test:e2e`) mocks HTTP and WebSocket behavior. It is
kept separate from `npm run test:e2e:integration`, which starts the real FastAPI
application, Vite frontend, cookie/CSRF authentication, SQLite metadata, a
disposable data root, and a deterministic provider implementing the existing
`ModelProvider` protocol. The integration provider never contacts Ollama. It
also supports deterministic cancellation, provider outage, and hostile native
tool-call scenarios.

For reproducible Chromium acceptance, the repository pins the official
Playwright `1.64.0` browser image and its digest. The Docker build copies the
checkout into the test image; the browser container receives no runtime host
workspace mounts. Run both suites with:

```sh
docker build -f web/e2e/Playwright.Dockerfile -t synai-phase13d-e2e:local .
docker run --rm \
  --network none \
  --cap-drop ALL \
  --security-opt no-new-privileges \
  --pids-limit 256 \
  --memory 3g \
  --cpus 2 \
  synai-phase13d-e2e:local \
  bash -lc 'npm run test:e2e && npm run test:e2e:integration'
```

The container runs as the unprivileged `pwuser`; it has no host workspace
volume, Docker/Podman socket, elevated capabilities, production credential, or
external network. API and Vite bind only to loopback inside that isolated
container. The integration API uses a temporary data root and is restarted by
its test supervisor to verify persistence and WebSocket reconnection. Test
logs record provider model/tool-schema counts, not prompts. Screenshots, video,
trace recording, and CI artifact uploads are disabled. The opt-in CI workflow
`.github/workflows/phase-13d-browser.yml` runs this isolated test image on
manual dispatch.

Project activity events are transactionally persisted and the WebSocket reads
the SQLite event log by cursor, so delivery is not dependent on an in-process
notification queue. Subscriber accounting is local to each service instance.
The web service's exclusive data-root ownership lock permits only one API
process to own a data root; multi-worker/multi-process deployment is therefore
not supported. Do not bypass the ownership lock to scale workers.

The browser suite checks WCAG 2.1 A/AA rules with axe-core at desktop, tablet,
and mobile sizes, including contrast checks, keyboard focus, responsive
navigation, reduced motion, long code blocks, live status, and keyboard
inspector resizing. These automated checks do not constitute complete
assistive-technology validation. Manual screen-reader testing remains a
separate acceptance item.

The optional Ollama smoke test is skipped by default and never runs in the
browser container:

```sh
SYNAI_RUN_OLLAMA_SMOKE=1 \
SYNAI_OLLAMA_SMOKE_URL=http://127.0.0.1:11434 \
SYNAI_OLLAMA_SMOKE_MODEL=your-installed-model \
PYTHONPATH=tests ./.venv/bin/python -m unittest tests.test_web_ollama_smoke
```

Only enable it deliberately against a safe configured endpoint. It sends the
non-sensitive prompt “Reply with the single word OK.”, performs model
discovery and streaming, and verifies persisted conversation state after an
application restart. It does not download models or change Ollama settings.

## Phase 13D final acceptance results

Acceptance was run with Playwright `1.64.0` and Chromium build `1248`
(`156.0.8078.4`) from the digest-pinned
`mcr.microsoft.com/playwright:v1.64.0-noble` image. The isolated browser run
used `--network none`, dropped Linux capabilities, no-new-privileges, bounded
CPU/memory/PIDs, no host mounts or sockets, and no production credentials.
Chromium is provided by the pinned image; the test-only image adds Python
virtual-environment support and locked project dependencies.

Verified:

- Existing UI-mock Playwright suite: **3 passed, 0 failed**.
- Real FastAPI/React integration suite with disposable SQLite/data root and
  deterministic provider: **3 passed, 0 failed**. This includes cookie and
  CSRF authentication, model selection across conversations, streamed content
  and reasoning, saved-history reopen, cancellation, provider outage, tool-call
  rejection with zero tool schemas, hostile-HTML rendering, WebSocket
  reconnect/gap recovery, project activity, session revocation/logout, and API
  restart persistence.
- Project activity verification: cursor ordering, retained-history gap
  resynchronization, snapshot refresh, project authorization, server restart
  reconnection, and UI refresh from authoritative state passed. Focused Python
  tests cover Origin rejection and event/session limits.
- Axe WCAG 2.1 A/AA scans: **0 violations** at desktop, tablet, and mobile
  layouts. Browser assertions also covered visible keyboard focus, dialog
  focus trapping/restoration, inspector keyboard resizing, reduced motion,
  200% text scaling, and horizontally scrollable long code blocks.
- Frontend TypeScript typecheck and production build passed; component tests:
  **9 passed**. Playwright TypeScript sources typechecked.
- OpenAPI consistency test passed. Python source compilation and
  `git diff --check` passed.
- Full Python unittest suite: **611 tests ran, 590 passed, 21 skipped, 0
  failed**.

Not performed: manual screen-reader/assistive-technology validation, the
opt-in live Ollama smoke test, and the manually dispatched GitHub Actions
workflow. Real Ollama endpoint connectivity and behavior therefore remain
unverified. Activity delivery is verified only for the documented single API
process per data root; multi-worker deployment remains unsupported.
