import {
  Activity,
  ArrowUpRight,
  Bot,
  Check,
  ChevronRight,
  CircleHelp,
  Copy,
  Cpu,
  FolderKanban,
  MessageSquare,
  Plus,
  ShieldCheck,
  SlidersHorizontal,
  Smartphone,
  SquareTerminal,
  Waypoints,
  XCircle,
} from "lucide-react";
import {
  useEffect,
  useCallback,
  useRef,
  useState,
  type CSSProperties,
  type FormEvent,
  type KeyboardEvent,
  type ReactNode,
} from "react";
import { Link, useLocation, useNavigate, useParams } from "react-router-dom";
import { apiRequest, notifySessionExpired } from "./api/client";
import { useResource } from "./api/useResource";
import type {
  ChatConversationListResponse,
  ChatConversationResponse,
  ProjectActivityListResponse,
  DeviceListResponse,
  DeviceMetadataResponse,
  ExecutionTargetStatusResponse,
  LogicalProjectListResponse,
  LogicalProjectResponse,
  ModelListResponse,
  ProjectListResponse,
  SnapshotListResponse,
  TaskListResponse,
  WorkspaceBindingListResponse,
} from "./api/contracts";
import { useAuth } from "./auth/AuthContext";
import { MessageContent } from "./components/MessageContent";
import { Button } from "./components/ui/button";
import { Card, CardTitle } from "./components/ui/card";

const draftCache = new Map<string, string>();

export function clearChatDrafts() {
  draftCache.clear();
}

export function CommandCenter() {
  const projects = useResource<LogicalProjectListResponse>("/api/v1/logical-projects");
  const legacy = useResource<ProjectListResponse>("/api/v1/projects");
  const conversations = useResource<ChatConversationListResponse>("/api/v1/chat/sessions?limit=12");
  const devices = useResource<DeviceListResponse>("/api/v1/devices");
  const models = useResource<ModelListResponse>("/api/v1/models");
  const sandboxes = useResource<ExecutionTargetStatusResponse>("/api/v1/execution-targets");

  return (
    <section className="page-stack" aria-labelledby="command-center-title">
      <PageHeading eyebrow="Workspace overview" title="Project Command Center">
        Projects, conversations, and distributed-device metadata. Chat has no access to source files or execution tools.
      </PageHeading>
      <div className="dashboard-grid">
        <section className="dashboard-section dashboard-projects" aria-labelledby="projects-heading">
          <SectionHeading icon={<FolderKanban size={17} />} title="Projects" to="/projects" />
          <ResourceMessage loading={projects.loading} error={projects.error} stale={projects.stale} />
          {projects.data?.projects.length ? (
            <div className="project-list">
              {projects.data.projects.slice(0, 5).map((project) => (
                <ProjectOverview key={project.id} project={project} />
              ))}
            </div>
          ) : !projects.loading && !projects.error ? (
            <EmptyState title="No logical projects" detail="Create a logical project or register an existing source device when one is available." />
          ) : null}
          <ResourceMessage loading={legacy.loading} error={legacy.error} />
          {!!legacy.data?.projects.length && (
            <div className="legacy-projects">
              <h3>Legacy host-path registrations</h3>
              {legacy.data.projects.map((project) => (
                <div className="legacy-project" key={project.id}>
                  <div>
                    <strong>{project.name}</strong>
                    <span>Read-only compatibility record · host-path project</span>
                  </div>
                  <span className="status-pill status-neutral">{project.status}</span>
                </div>
              ))}
              <p className="small-note">These records do not grant new host filesystem authority and cannot be used as distributed projects.</p>
            </div>
          )}
        </section>

        <section className="dashboard-section" aria-labelledby="recent-conversations-heading">
          <SectionHeading icon={<MessageSquare size={17} />} title="Recent conversations" to="/chat" />
          <ResourceMessage loading={conversations.loading} error={conversations.error} stale={conversations.stale} />
          {conversations.data?.conversations.length ? (
            <ul className="compact-list">
              {conversations.data.conversations.slice(0, 6).map((conversation) => (
                <li key={conversation.id}>
                  <Link to={`/chat/${conversation.id}`} className="list-link">
                    <span className="list-main">
                      <strong>{conversation.title || "New conversation"}</strong>
                      <span>{conversation.model || "Model not selected"} · {conversation.project_id ? "Project chat" : "General chat"}</span>
                    </span>
                    <span className={`status-pill ${statusClass(conversation.state)}`}>{conversation.state}</span>
                  </Link>
                </li>
              ))}
            </ul>
          ) : !conversations.loading && !conversations.error ? (
            <EmptyState title="No conversations yet" detail="Start a general chat without registering a project." action={<Button asChild><Link to="/chat">Start a chat</Link></Button>} />
          ) : null}
        </section>

        <section className="dashboard-section" aria-labelledby="tasks-heading">
          <SectionHeading icon={<Bot size={17} />} title="Agent Tasks" to="/agent-tasks" />
          <p className="muted-copy">Execution is disabled until Phase 13F.</p>
          {projects.data?.projects.map((project) => (
            <TaskSummary key={project.id} project={project} />
          ))}
          {!projects.loading && !projects.error && projects.data?.projects.length === 0 && (
            <EmptyState title="No executable Agent Tasks" detail="Task execution is not available in this phase." />
          )}
        </section>

        <section className="dashboard-section" aria-labelledby="devices-heading">
          <SectionHeading icon={<Smartphone size={17} />} title="Devices" to="/devices" />
          <ResourceMessage loading={devices.loading} error={devices.error} stale={devices.stale} />
          {devices.data?.devices.length ? (
            <ul className="compact-list">
              {devices.data.devices.slice(0, 4).map((device) => <DeviceSummary device={device} key={device.id} />)}
            </ul>
          ) : !devices.loading && !devices.error ? (
            <EmptyState title="No registered devices" detail="A registered device is not an operational Client Agent." />
          ) : null}
        </section>

        <section className="dashboard-section" aria-labelledby="models-heading">
          <SectionHeading icon={<Cpu size={17} />} title="Models and service health" />
          {models.loading ? <p className="muted-copy">Checking Ollama model availability…</p> : null}
          {models.error ? (
            <StatusMessage tone="warning" title="Ollama unavailable">
              Saved projects and conversations remain available. Reconnect the configured Ollama service to start a turn.
            </StatusMessage>
          ) : models.data ? (
            <div className="service-summary">
              <span className="status-dot status-dot-good" />
              <div><strong>{models.data.models.length ? "Ollama reachable" : "Ollama reachable; no models found"}</strong>
                <span>{models.data.models.length ? `${models.data.models.length} model${models.data.models.length === 1 ? "" : "s"} available` : "Install or make a chat model available to start a conversation."}</span>
              </div>
            </div>
          ) : null}
        </section>

        <section className="dashboard-section" aria-labelledby="sandboxes-heading">
          <SectionHeading icon={<SquareTerminal size={17} />} title="Sandboxes" to="/sandboxes" />
          <ResourceMessage error={sandboxes.error} />
          {sandboxes.data && (
            <StatusMessage tone={sandboxes.data.broker_available ? "warning" : "neutral"} title="Execution disabled">
              {sandboxes.data.broker_available
                ? "Broker metadata is available, but chat and Agent Task execution remain disabled."
                : "Sandbox Broker is not installed. No containers or execution targets are running."}
            </StatusMessage>
          )}
        </section>
      </div>
    </section>
  );
}

function ProjectOverview({ project }: { project: LogicalProjectResponse }) {
  const snapshots = useResource<SnapshotListResponse>(`/api/v1/logical-projects/${project.id}/snapshots`);
  const bindings = useResource<WorkspaceBindingListResponse>(`/api/v1/logical-projects/${project.id}/bindings`);
  const latest = snapshots.data?.snapshots[0];
  return (
    <article className="project-overview">
      <div className="project-overview-title">
        <span className="project-mark"><FolderKanban size={17} /></span>
        <div><strong>{project.name}</strong><span>Logical distributed project</span></div>
        <Button asChild variant="ghost" aria-label={`Open ${project.name} Workbench`}>
          <Link to={`/workbench/${project.id}`}><ArrowUpRight size={16} /></Link>
        </Button>
      </div>
      <div className="project-facts">
        <span><b>Source devices</b> {bindings.data?.bindings.length ?? (bindings.loading ? "Loading…" : "None")}</span>
        <span><b>Latest snapshot</b> {latest ? `${latest.state} · ${dateLabel(latest.created_at)}` : snapshots.loading ? "Loading…" : snapshots.error ? "Unavailable" : "None"}</span>
        <span><b>Project created</b> {dateLabel(project.created_at)}</span>
      </div>
      {(snapshots.error || bindings.error) && <p className="small-note">Some project status data could not be loaded.</p>}
    </article>
  );
}

function TaskSummary({ project }: { project: LogicalProjectResponse }) {
  const tasks = useResource<TaskListResponse>(`/api/v1/logical-projects/${project.id}/tasks`);
  if (!tasks.data && !tasks.loading && tasks.error) return <p className="small-note">{project.name}: task status unavailable.</p>;
  if (!tasks.data?.tasks.length) return null;
  return <p className="small-note">{project.name}: {tasks.data.tasks.length} saved task record(s), execution unavailable.</p>;
}

function DeviceSummary({ device }: { device: DeviceMetadataResponse }) {
  return (
    <li className="device-summary">
      <span className={`status-dot ${device.state === "authorized" ? "status-dot-good" : device.state === "revoked" ? "status-dot-bad" : ""}`} />
      <span className="list-main"><strong>{deviceName(device)}</strong>
        <span>{device.state} · {device.recently_active ? "recent authenticated activity" : "no recent authenticated activity"}</span>
      </span>
    </li>
  );
}

export function ProjectsPage() {
  const logical = useResource<LogicalProjectListResponse>("/api/v1/logical-projects");
  const legacy = useResource<ProjectListResponse>("/api/v1/projects");
  const auth = useAuth();
  const [name, setName] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);

  const create = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!auth.csrfToken || !name.trim()) return;
    setCreating(true);
    setError(null);
    try {
      const bytes = crypto.getRandomValues(new Uint8Array(24));
      const key = Array.from(bytes, (value) => value.toString(16).padStart(2, "0")).join("");
      await apiRequest<LogicalProjectResponse>("/api/v1/logical-projects", {
        method: "POST",
        body: JSON.stringify({ name: name.trim(), registration_key: key }),
      }, auth.csrfToken);
      setName("");
      logical.reload();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Project could not be created.");
    } finally {
      setCreating(false);
    }
  };
  return (
    <section className="page-stack">
      <PageHeading eyebrow="Project registry" title="Projects">
        Logical project identities organize metadata only. Creating one does not register a device, expose files, or grant a workspace mount.
      </PageHeading>
      <Card>
        <CardTitle>Create logical project</CardTitle>
        <form className="inline-form" onSubmit={(event) => void create(event)}>
          <label className="sr-only" htmlFor="project-name">Project name</label>
          <input id="project-name" value={name} onChange={(event) => setName(event.target.value)} maxLength={128} placeholder="Project name" required />
          <Button disabled={creating || !auth.csrfToken}>{creating ? "Creating…" : "Create project"}</Button>
        </form>
        {error && <ErrorNotice message={error} />}
      </Card>
      <ResourceMessage loading={logical.loading} error={logical.error} stale={logical.stale} />
      {logical.data?.projects.length ? (
        <div className="project-cards">
          {logical.data.projects.map((project) => (
            <ProjectOverview key={project.id} project={project} />
          ))}
        </div>
      ) : !logical.loading && !logical.error ? (
        <EmptyState title="No logical projects" detail="Create a project identity above. A source device and snapshots are separate, explicit registrations." />
      ) : null}
      <section className="subsection">
        <h2>Legacy host-path projects</h2>
        <p className="muted-copy">Compatibility registrations remain read-only and are not converted into distributed projects.</p>
        <ResourceMessage loading={legacy.loading} error={legacy.error} />
        {legacy.data?.projects.length ? legacy.data.projects.map((project) => (
          <div className="legacy-project" key={project.id}>
            <div><strong>{project.name}</strong><span>Host-path compatibility record · {project.access}</span></div>
            <span className="status-pill status-neutral">{project.status}</span>
          </div>
        )) : !legacy.loading && !legacy.error ? <EmptyState title="No legacy project registrations" detail="No operator-configured host-path project is registered." /> : null}
      </section>
    </section>
  );
}

export function WorkbenchPage() {
  const { projectId = "" } = useParams();
  const location = useLocation();
  const project = useResource<LogicalProjectResponse>(`/api/v1/logical-projects/${projectId}`);
  const bindings = useResource<WorkspaceBindingListResponse>(`/api/v1/logical-projects/${projectId}/bindings`);
  const snapshots = useResource<SnapshotListResponse>(`/api/v1/logical-projects/${projectId}/snapshots`);
  const activity = useResource<ProjectActivityListResponse>(`/api/v1/logical-projects/${projectId}/activity?limit=100`);
  const tasks = useResource<TaskListResponse>(`/api/v1/logical-projects/${projectId}/tasks`);
  const conversations = useResource<ChatConversationListResponse>(`/api/v1/chat/sessions?project_id=${projectId}`);
  const [detailOpen, setDetailOpen] = useState(true);
  const [detailWidth, setDetailWidth] = useState(304);
  const [panel, setPanel] = useState<"project" | "chat" | "snapshots" | "activity" | "memory">(
    () => location.pathname.includes("/chat") ? "chat" : "project",
  );
  const refreshProjectData = useCallback(() => {
    activity.reload();
    project.reload();
    bindings.reload();
    snapshots.reload();
    tasks.reload();
  }, [activity.reload, bindings.reload, project.reload, snapshots.reload, tasks.reload]);
  const style = { "--inspector-width": `${detailWidth}px` } as CSSProperties;
  if (project.error && !project.data) return <ErrorPage title="Project unavailable" message={project.error} />;
  return (
    <section className="page-stack workbench-page">
      <PageHeading eyebrow="Developer Workbench" title={project.data?.name ?? (project.loading ? "Loading project…" : "Project Workbench")}>
        Project-scoped chat and distributed metadata. This Workbench does not access a source workspace or execute tasks.
      </PageHeading>
      <div className="workbench-toolbar">
        <span className="status-pill status-neutral">Project {projectId.slice(0, 8)}</span>
        <div className="mode-links" aria-label="Workbench modes">
          <Link className="mode-link" to={`/projects/${projectId}/chat`}><MessageSquare size={15} /> Chat</Link>
          <Link className="mode-link" to={`/projects/${projectId}/agent-tasks`}><Bot size={15} /> Agent Tasks <span className="disabled-label">disabled</span></Link>
        </div>
        <Button variant="secondary" className="inspector-toggle" onClick={() => setDetailOpen((open) => !open)} aria-expanded={detailOpen}>
          {detailOpen ? "Hide details" : "Show details"}
        </Button>
      </div>
      <div className={`workbench-grid ${detailOpen ? "" : "workbench-no-inspector"}`} style={style}>
        <nav className="workbench-navigation" aria-label="Project navigation">
          <span className="eyebrow">Project</span>
          <button type="button" className={panel === "project" ? "selected" : ""} onClick={() => setPanel("project")}><FolderKanban size={16} />Overview</button>
          <button type="button" className={panel === "chat" ? "selected" : ""} onClick={() => setPanel("chat")}><MessageSquare size={16} />Chat</button>
          <button type="button" className={panel === "snapshots" ? "selected" : ""} onClick={() => setPanel("snapshots")}><Copy size={16} />Snapshots</button>
          <button type="button" className={panel === "activity" ? "selected" : ""} onClick={() => setPanel("activity")}><Activity size={16} />Activity</button>
          <button type="button" className={panel === "memory" ? "selected" : ""} onClick={() => setPanel("memory")}><CircleHelp size={16} />Memory status</button>
          <hr />
          <Link to="/projects"><ChevronRight size={15} />All projects</Link>
          <Link to={`/projects/${projectId}/chat`}><MessageSquare size={15} />Conversations</Link>
        </nav>
        <section className="workbench-main-pane" aria-label="Workbench content">
          {panel === "project" && (
            <>
              <div className="pane-heading"><div><span className="eyebrow">Project overview</span><h2>{project.data?.name ?? "Project details"}</h2></div>
                <Button asChild><Link to={`/projects/${projectId}/chat`}><MessageSquare size={16} />Open project chat</Link></Button>
              </div>
              <div className="workbench-content-grid">
                <InfoCard title="Source Device" value={bindings.data?.bindings.length ? `${bindings.data.bindings.length} workspace binding(s)` : "No device bindings"} detail="Binding metadata only; no Client Agent is connected." loading={bindings.loading} error={bindings.error} />
                <InfoCard title="Execution Target" value="Unavailable" detail="Sandbox Broker is not installed; no task can be executed." />
                <InfoCard title="Apply Destination" value="Unavailable" detail="No client-side apply destination exists in this phase." />
                <InfoCard title="Snapshots" value={snapshots.data ? `${snapshots.data.snapshots.length} retained record(s)` : "No snapshots"} detail="Immutable source metadata; no source files are exposed to Chat." loading={snapshots.loading} error={snapshots.error} />
                <InfoCard title="Agent Tasks" value={tasks.data ? `${tasks.data.tasks.length} saved record(s)` : "No task records"} detail="Execution remains disabled until Phase 13F." loading={tasks.loading} error={tasks.error} />
                <InfoCard title="Project conversations" value={conversations.data ? `${conversations.data.conversations.length} conversation(s)` : "No conversations"} detail="Project association is metadata only." loading={conversations.loading} error={conversations.error} />
              </div>
            </>
          )}
          {panel === "chat" && <ChatPage embedded />}
          {panel === "snapshots" && <SnapshotPanel data={snapshots.data} loading={snapshots.loading} error={snapshots.error} />}
          {panel === "activity" && <ActivityPanel
            projectId={projectId}
            data={activity.data}
            loading={activity.loading}
            error={activity.error}
            reload={refreshProjectData}
          />}
          {panel === "memory" && <StatusMessage tone="neutral" title="Project memory unavailable">Phase 12 memory is not automatically associated with logical projects. No memory content is read or migrated here.</StatusMessage>}
        </section>
        {detailOpen && (
          <aside className="details-pane" aria-label="Project details inspector">
            <div className="inspector-heading"><span className="eyebrow">Inspector</span><ShieldCheck size={16} /></div>
            <h2>Project context</h2>
            <dl className="detail-list">
              <dt>Identity</dt><dd>{project.data?.id ?? projectId}</dd>
              <dt>Project type</dt><dd>Logical distributed project</dd>
              <dt>Created</dt><dd>{project.data ? dateLabel(project.data.created_at) : "—"}</dd>
              <dt>Source binding</dt><dd>{bindings.data?.bindings[0]?.name ?? "Not registered"}</dd>
              <dt>Snapshot state</dt><dd>{snapshots.data?.snapshots[0]?.state ?? "No snapshots"}</dd>
            </dl>
            <label className="resize-control" htmlFor="inspector-size">Inspector width</label>
            <input id="inspector-size" type="range" min={248} max={480} step={8} value={detailWidth} onChange={(event) => setDetailWidth(Number(event.target.value))} />
            <p className="small-note">Use the arrow keys to resize this panel.</p>
            {tasks.data?.execution_available === false && <p className="boundary-note">Task execution is unavailable.</p>}
          </aside>
        )}
      </div>
    </section>
  );
}

function InfoCard({ title, value, detail, loading, error }: { title: string; value: string; detail: string; loading?: boolean; error?: string | null }) {
  return <Card className="info-card"><span className="eyebrow">{title}</span><CardTitle>{loading ? "Loading…" : value}</CardTitle><p className="muted-copy">{error ? "Status is unavailable." : detail}</p></Card>;
}

function SnapshotPanel({ data, loading, error }: { data: SnapshotListResponse | null; loading: boolean; error: string | null }) {
  return <div><span className="eyebrow">Snapshot history</span><h2>Immutable snapshots</h2><ResourceMessage loading={loading} error={error} />
    {data?.snapshots.length ? <ul className="compact-list">{data.snapshots.map((snapshot) => <li key={snapshot.snapshot_id}>
      <span className="list-main"><strong>{snapshot.snapshot_id.slice(0, 12)}</strong><span>{dateLabel(snapshot.created_at)} · {formatBytes(snapshot.total_bytes)}</span></span><span className={`status-pill ${statusClass(snapshot.state)}`}>{snapshot.state}</span>
    </li>)}</ul> : !loading && !error ? <EmptyState title="No snapshots" detail="Snapshots require a future consent-based Client Agent flow." /> : null}
  </div>;
}

function ActivityPanel({ projectId, data, loading, error, reload }: {
  projectId: string;
  data: ProjectActivityListResponse | null;
  loading: boolean;
  error: string | null;
  reload: () => void;
}) {
  const [streamStatus, setStreamStatus] = useState("connecting");
  const cursor = useRef<number | null>(null);
  useEffect(() => {
    cursor.current = null;
  }, [projectId]);
  useEffect(() => {
    if (!data || data.project_id !== projectId) return;
    let active = true;
    let socket: WebSocket | null = null;
    let retry: number | undefined;
    let delay = 700;
    if (cursor.current === null) cursor.current = data.cursor;
    const connect = () => {
      if (!active) return;
      setStreamStatus(cursor.current ? "reconnecting" : "connecting");
      const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
      socket = new WebSocket(
        `${protocol}//${window.location.host}/api/v1/events/v1/projects/${projectId}?after=${cursor.current ?? 0}`,
      );
      socket.onopen = () => {
        delay = 700;
        setStreamStatus("connected");
      };
      socket.onmessage = (message) => {
        let event: ProjectActivityEvent;
        try {
          event = JSON.parse(message.data) as ProjectActivityEvent;
        } catch {
          setStreamStatus("resynchronizing");
          reload();
          return;
        }
        if (event.schema_version !== 1 || event.project_id !== projectId) return;
        if (event.type === "project_snapshot" || event.type === "resynchronization_required") {
          cursor.current = Math.max(cursor.current ?? 0, event.event_id);
          setStreamStatus(event.type === "project_snapshot" ? "connected" : "resynchronizing");
          reload();
          return;
        }
        const previous = cursor.current ?? 0;
        if (event.event_id <= previous) return;
        cursor.current = event.event_id;
        if (event.event_id !== previous + 1) setStreamStatus("resynchronizing");
        reload();
      };
      socket.onclose = (event) => {
        if (!active) return;
        if (event.code === 4401) {
          notifySessionExpired();
          setStreamStatus("session expired");
          return;
        }
        if (event.code === 4404) {
          setStreamStatus("project unavailable");
          return;
        }
        setStreamStatus("reconnecting");
        retry = window.setTimeout(connect, delay);
        delay = Math.min(delay * 2, 10_000);
      };
      socket.onerror = () => setStreamStatus("reconnecting");
    };
    connect();
    return () => {
      active = false;
      if (retry) window.clearTimeout(retry);
      socket?.close();
    };
  }, [data?.project_id, projectId, reload]);

  const entries = data?.events ?? [];
  return <div>
    <span className="eyebrow">Project history</span><h2>Activity</h2>
    <p className="small-note" role="status" aria-live="polite">
      {loading ? "Loading persisted activity…" : error ? `Activity unavailable: ${error}` : `Live stream ${streamStatus}.`}
    </p>
    {entries.length ? <ul className="compact-list">{entries.map((entry) => (
      <li key={entry.event_id}>
        <span className="list-main">
          <strong>{activityLabel(entry.type, entry.payload)}</strong>
          <span>{dateLabel(entry.created_at)}</span>
        </span>
        <span className="status-pill status-neutral">#{entry.event_id}</span>
      </li>
    ))}</ul> : !loading && !error ? <EmptyState title="No project activity" detail="Persisted project, binding, and snapshot changes will appear here." /> : null}
  </div>;
}

type ProjectActivityEvent = ProjectActivityListResponse["events"][number];

export function ChatPage({ embedded = false }: { embedded?: boolean }) {
  const { projectId, conversationId } = useParams();
  const navigate = useNavigate();
  const auth = useAuth();
  const models = useResource<ModelListResponse>("/api/v1/models");
  const historyPath = projectId
    ? `/api/v1/chat/sessions?project_id=${encodeURIComponent(projectId)}`
    : "/api/v1/chat/sessions?limit=100";
  const history = useResource<ChatConversationListResponse>(historyPath);
  const current = useResource<ChatConversationResponse>(
    conversationId ? `/api/v1/chat/sessions/${conversationId}` : null,
  );
  const [liveSession, setLiveSession] = useState<ChatConversationResponse | null>(null);
  const [modelOverride, setModelOverride] = useState<string | null>(null);
  const [sending, setSending] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [eventStatus, setEventStatus] = useState<"connecting" | "connected" | "reconnecting" | "offline">("offline");
  const [draft, setDraft] = useState(() => draftCache.get(draftKey(conversationId, projectId)) ?? "");
  const composer = useRef<HTMLTextAreaElement>(null);
  const scrollRegion = useRef<HTMLDivElement>(null);
  const nearBottom = useRef(true);
  const cursor = useRef(0);
  const currentData = useRef(current.data);
  const routeConversation = useRef(conversationId ?? "");
  routeConversation.current = conversationId ?? "";
  useEffect(() => {
    currentData.current = current.data;
  }, [current.data]);
  const session = liveSession?.id === conversationId ? liveSession : current.data;
  const messages = session?.messages ?? [];
  const selectedModel = modelOverride ?? (
    (conversationId && current.data?.id === conversationId && current.data.model)
      ? current.data.model
      : models.data?.models[0]?.name ?? ""
  );
  const running = session?.state === "running" || sending;

  useEffect(() => {
    setDraft(draftCache.get(draftKey(conversationId, projectId)) ?? "");
    setLiveSession(null);
    setModelOverride(null);
    setNotice(null);
  }, [conversationId, projectId]);

  useEffect(() => {
    if (current.data?.id === conversationId) setLiveSession(current.data);
  }, [current.data, conversationId]);

  useEffect(() => {
    if (nearBottom.current && scrollRegion.current) {
      scrollRegion.current.scrollTop = scrollRegion.current.scrollHeight;
    }
  }, [messages]);

  useEffect(() => {
    if (!conversationId) {
      setEventStatus("offline");
      return;
    }
    let active = true;
    let socket: WebSocket | null = null;
    let retry: number | undefined;
    let delay = 700;
    cursor.current = 0;
    const refresh = () => current.reload();
    const connect = () => {
      if (!active) return;
      setEventStatus(cursor.current ? "reconnecting" : "connecting");
      const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
      const after = cursor.current ? `?after=${cursor.current}` : "";
      socket = new WebSocket(`${protocol}//${window.location.host}/api/v1/events/v1/chat/${conversationId}${after}`);
      socket.onopen = () => {
        delay = 700;
        setEventStatus("connected");
      };
      socket.onmessage = (message) => {
        let event: ChatEvent;
        try {
          event = JSON.parse(message.data) as ChatEvent;
        } catch {
          setNotice("Live update was invalid. Reload the saved conversation.");
          return;
        }
        if (event.schema_version !== 1 || event.conversation_id !== conversationId) return;
        if (event.type === "resynchronization_required") {
          cursor.current = event.event_id;
          refresh();
          return;
        }
        if (event.type === "session_snapshot") {
          cursor.current = Math.max(cursor.current, event.event_id);
          refresh();
          return;
        }
        if (event.event_id <= cursor.current) return;
        if (event.event_id !== cursor.current + 1) {
          cursor.current = event.event_id;
          refresh();
          setNotice("Some live events were missed. Reloaded the saved conversation.");
          return;
        }
        cursor.current = event.event_id;
        if (event.type === "turn_started") {
          setLiveSession((existing) => {
            const base = existing?.id === conversationId ? existing : currentData.current;
            if (!base) return base;
            const updated = { ...base, state: "running" as const, messages: [...(base.messages ?? [])] };
            const last = updated.messages.at(-1);
            if (last?.role !== "assistant" || last.status !== "streaming") {
              updated.messages.push({
                role: "assistant", content: "", thinking: "", status: "streaming",
                created_at: new Date().toISOString(),
              });
            }
            return updated;
          });
          refresh();
        } else if (event.type === "content_delta" || event.type === "thinking_delta") {
          const text = typeof event.payload.text === "string" ? event.payload.text : "";
          setLiveSession((existing) => {
            const base = existing?.id === conversationId ? existing : currentData.current;
            if (!base) return base;
            const updated = { ...base, state: "running" as const, messages: [...(base.messages ?? [])] };
            let assistantIndex = -1;
            for (let index = updated.messages.length - 1; index >= 0; index -= 1) {
              if (updated.messages[index].role === "assistant") {
                assistantIndex = index;
                break;
              }
            }
            if (assistantIndex < 0 || updated.messages.at(-1)?.role !== "assistant") {
              updated.messages.push({
                role: "assistant", content: "", thinking: "", status: "streaming",
                created_at: new Date().toISOString(),
              });
              assistantIndex = updated.messages.length - 1;
            }
            const target = updated.messages[assistantIndex];
            updated.messages[assistantIndex] = {
              ...target,
              content: event.type === "content_delta" ? target.content + text : target.content,
              thinking: event.type === "thinking_delta" ? target.thinking + text : target.thinking,
              status: "streaming",
            };
            return updated;
          });
        } else if (event.type === "provider_unavailable"
          || event.type.startsWith("turn_")) {
          refresh();
          if (event.type === "turn_completed") setNotice("Response complete.");
          if (event.type === "turn_cancelled") setNotice("Generation stopped. Partial response was saved.");
          if (event.type === "turn_interrupted") setNotice("Previous response was interrupted and saved; it will not be replayed.");
          if (event.type === "turn_failed") setNotice("The response could not be completed. Partial output was saved.");
          if (event.type === "provider_unavailable") setNotice("Ollama is unavailable. Your conversation is saved; try again when the provider is available.");
        }
      };
      socket.onclose = (event) => {
        if (!active) return;
        if (event.code === 4401) {
          notifySessionExpired();
          setEventStatus("offline");
          setNotice("Your browser session expired. Sign in again to continue.");
          return;
        }
        setEventStatus("reconnecting");
        refresh();
        retry = window.setTimeout(connect, delay);
        delay = Math.min(delay * 2, 10_000);
      };
      socket.onerror = () => setEventStatus("reconnecting");
    };
    connect();
    return () => {
      active = false;
      if (retry) window.clearTimeout(retry);
      socket?.close();
    };
  }, [conversationId, current.reload]);

  const setDraftValue = (value: string) => {
    setDraft(value);
    draftCache.set(draftKey(conversationId, projectId), value);
  };
  const routeFor = (id?: string) => embedded
    ? `/workbench/${projectId}/chat${id ? `/${id}` : ""}`
    : projectId
      ? `/projects/${projectId}/chat${id ? `/${id}` : ""}`
      : `/chat${id ? `/${id}` : ""}`;

  const send = async () => {
    if (!draft.trim() || running || !auth.csrfToken) return;
    if (!selectedModel) {
      setNotice(models.error ? "Ollama is unavailable. Your draft is preserved." : "Select an available chat model first.");
      return;
    }
    setSending(true);
    setNotice(null);
    let activeId = conversationId;
    const prompt = draft;
    const originalDraftKey = draftKey(conversationId, projectId);
    try {
      if (!activeId) {
        const created = await apiRequest<ChatConversationResponse>("/api/v1/chat/sessions", {
          method: "POST",
          body: JSON.stringify({ model: selectedModel, project_id: projectId ?? null }),
        }, auth.csrfToken);
        activeId = created.id;
        draftCache.set(draftKey(activeId, projectId), prompt);
        navigate(routeFor(activeId));
        history.reload();
      }
      await apiRequest(`/api/v1/chat/sessions/${activeId}/turns`, {
        method: "POST",
        body: JSON.stringify({ prompt, model: selectedModel }),
      }, auth.csrfToken);
      draftCache.set(draftKey(activeId, projectId), "");
      draftCache.set(originalDraftKey, "");
      if (routeConversation.current === activeId) {
        setDraft("");
        setLiveSession((existing) => {
          const base = existing?.id === activeId ? existing : currentData.current;
          if (!base || base.id !== activeId) return existing;
          const updated = { ...base, state: "running" as const, messages: [...(base.messages ?? [])] };
          const last = updated.messages.at(-1);
          if (last?.role !== "user" || last.content !== prompt) {
            updated.messages.push({
              role: "user", content: prompt, thinking: "", status: "complete",
              created_at: new Date().toISOString(),
            });
          }
          if (updated.messages.at(-1)?.role !== "assistant") {
            updated.messages.push({
              role: "assistant", content: "", thinking: "", status: "streaming",
              created_at: new Date().toISOString(),
            });
          }
          return updated;
        });
        current.reload();
        setNotice("Generating response…");
        composer.current?.focus();
      }
    } catch (cause) {
      if (activeId) draftCache.set(draftKey(activeId, projectId), prompt);
      if (routeConversation.current === (activeId ?? "")) {
        setDraft(prompt);
        setNotice(cause instanceof Error ? cause.message : "The message could not be sent. Your draft is preserved.");
      }
    } finally {
      setSending(false);
    }
  };

  const stop = async () => {
    if (!conversationId || !auth.csrfToken) return;
    try {
      await apiRequest(`/api/v1/chat/sessions/${conversationId}/cancel`, { method: "POST" }, auth.csrfToken);
      setNotice("Stopping generation…");
    } catch (cause) {
      setNotice(cause instanceof Error ? cause.message : "Generation could not be stopped.");
    }
  };

  const onComposerKey = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      void send();
    }
  };

  const conversations = history.data?.conversations ?? [];
  const availableModels = models.data?.models ?? [];
  return (
    <section className={`chat-layout${embedded ? " chat-layout-embedded" : ""}`} aria-label="Browser chat">
      <aside className="chat-sidebar">
        <div className="chat-sidebar-heading"><span className="eyebrow">{projectId ? "Project conversations" : "Your conversations"}</span>
          <Button variant="secondary" onClick={() => navigate(routeFor())}><Plus size={16} />New chat</Button>
        </div>
        <ResourceMessage loading={history.loading} error={history.error} stale={history.stale} />
        <nav className="conversation-list" aria-label="Conversation history">
          {conversations.map((item) => (
            <Link key={item.id} to={routeFor(item.id)} className={`conversation-link ${item.id === conversationId ? "active" : ""}`}>
              <MessageSquare size={15} aria-hidden="true" />
              <span><strong>{item.title || "New conversation"}</strong><small>{item.model || "No model selected"} · {dateLabel(item.updated_at)}</small></span>
            </Link>
          ))}
          {!history.loading && !history.error && conversations.length === 0 && <p className="small-note">No saved conversations yet.</p>}
        </nav>
      </aside>
      <div className="chat-main">
        <header className="chat-toolbar">
          <div><span className="eyebrow">{projectId ? "Project context · metadata only" : "General conversation"}</span>
            <h1>{session?.title || (conversationId ? "Conversation" : "New conversation")}</h1></div>
          <label className="model-picker"><span>Model</span>
            <select value={selectedModel} onChange={(event) => setModelOverride(event.target.value)} disabled={models.loading || availableModels.length === 0}>
              {!availableModels.length && <option value="">{models.error ? "Ollama unavailable" : "No models available"}</option>}
              {session?.model && !availableModels.some((model) => model.name === session.model)
                && <option value={session.model}>{session.model} (unavailable)</option>}
              {availableModels.map((model) => <option value={model.name} key={model.name}>{model.name}</option>)}
            </select>
          </label>
        </header>
        <ResourceMessage loading={current.loading} error={current.error} />
        {models.error && <StatusMessage tone="warning" title="Model provider unavailable">Saved conversation history remains available; your draft will not be discarded.</StatusMessage>}
        <div
          ref={scrollRegion}
          className="message-scroll-region"
          onScroll={(event) => {
            const node = event.currentTarget;
            nearBottom.current = node.scrollHeight - node.scrollTop - node.clientHeight < 96;
          }}
          aria-label="Conversation messages"
        >
          {messages.length === 0 ? (
            <div className="chat-welcome">
              <div className="welcome-mark"><MessageSquare size={22} /></div>
              <h2>{projectId ? "Chat about your project" : "What would you like to talk about?"}</h2>
              <p>{projectId ? "The project reference is metadata only. No source files or client workspace are provided to the model." : "Start a general conversation. A project registration is not required."}</p>
            </div>
          ) : messages.map((message, index) => (
            <article className={`message-row message-${message.role}`} key={`${message.created_at}-${index}`}>
              <div className="message-avatar" aria-hidden="true">{message.role === "assistant" ? <Bot size={16} /> : <span>You</span>}</div>
              <div className="message-body">
                <div className="message-meta"><strong>{message.role === "assistant" ? "SynAI" : "You"}</strong>
                  <span>{message.status === "streaming" ? "Generating…" : message.status !== "complete" ? message.status : ""}</span>
                  <CopyMessageButton content={message.content} />
                </div>
                {message.content ? <MessageContent content={message.content} /> : message.status === "streaming" ? <span className="typing-indicator" aria-hidden="true"><i /><i /><i /></span> : null}
                {message.thinking && <details className="thinking-details"><summary>Model reasoning</summary><p>{message.thinking}</p></details>}
              </div>
            </article>
          ))}
          <div className="scroll-anchor" />
        </div>
        <div className="chat-composer-area">
          <div className="chat-live-status" role="status" aria-live="polite">{notice || (eventStatus === "reconnecting" ? "Reconnecting to live updates…" : eventStatus === "connected" && running ? "Response is streaming." : "")}</div>
          <form className="chat-composer" onSubmit={(event) => { event.preventDefault(); void send(); }}>
            <label className="sr-only" htmlFor="chat-draft">Message</label>
            <textarea
              ref={composer}
              id="chat-draft"
              value={draft}
              onChange={(event) => setDraftValue(event.target.value)}
              onKeyDown={onComposerKey}
              rows={3}
              maxLength={32_768}
              placeholder="Message SynAI…"
              aria-describedby="composer-help"
              disabled={sending}
            />
            <div className="composer-footer">
              <span id="composer-help">Enter to send · Shift+Enter for a new line</span>
              {running && conversationId
                ? <Button type="button" variant="secondary" onClick={() => void stop()}><XCircle size={16} />Stop generation</Button>
                : <Button type="submit" disabled={!draft.trim() || sending || !auth.csrfToken}><MessageSquare size={16} />{sending ? "Sending…" : "Send"}</Button>}
            </div>
          </form>
          <p className="composer-boundary">Chat has no tools, terminal, filesystem, or patch authority. Model output may be inaccurate.</p>
        </div>
      </div>
    </section>
  );
}

function CopyMessageButton({ content }: { content: string }) {
  const [copied, setCopied] = useState(false);
  const [error, setError] = useState(false);
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(content);
      setCopied(true);
      setError(false);
      window.setTimeout(() => setCopied(false), 1500);
    } catch {
      setError(true);
    }
  };
  if (!content) return null;
  return <button type="button" className="copy-button message-copy" onClick={() => void copy()} aria-label="Copy message">
    {copied ? <Check size={14} /> : <Copy size={14} />}{error ? "Unavailable" : copied ? "Copied" : "Copy"}
  </button>;
}

type ChatEvent = {
  schema_version: 1;
  event_id: number;
  conversation_id: string;
  type:
    | "session_snapshot"
    | "turn_started"
    | "content_delta"
    | "thinking_delta"
    | "turn_completed"
    | "turn_cancelled"
    | "turn_interrupted"
    | "turn_failed"
    | "provider_unavailable"
    | "resynchronization_required";
  payload: Record<string, unknown>;
};

function draftKey(conversationId?: string, projectId?: string) {
  return conversationId ?? `new:${projectId ?? "general"}`;
}

export function AgentTasksPage() {
  const projects = useResource<LogicalProjectListResponse>("/api/v1/logical-projects");
  return <section className="page-stack"><PageHeading eyebrow="Workflow metadata" title="Agent Tasks">Persisted task contracts are visible for transparency. Creating or executing tasks is disabled until Phase 13F.</PageHeading>
    <StatusMessage tone="neutral" title="Task execution disabled">This page does not plan, dispatch, approve, or execute agent work.</StatusMessage>
    <ResourceMessage loading={projects.loading} error={projects.error} />
    {projects.data?.projects.map((project) => <TaskProject key={project.id} project={project} />)}
    {!projects.loading && !projects.error && !projects.data?.projects.length && <EmptyState title="No Agent Task records" detail="No logical projects are registered; execution controls are not available in Phase 13D." />}
  </section>;
}

function TaskProject({ project }: { project: LogicalProjectResponse }) {
  const tasks = useResource<TaskListResponse>(`/api/v1/logical-projects/${project.id}/tasks`);
  return <section className="subsection"><h2>{project.name}</h2><ResourceMessage loading={tasks.loading} error={tasks.error} />
    {tasks.data?.tasks.length ? <ul className="compact-list">{tasks.data.tasks.map((task) => <li key={task.task_id}>
      <span className="list-main"><strong>{task.task_id.slice(0, 12)}</strong><span>Snapshot {task.source_snapshot_id.slice(0, 12)} · execution unavailable</span></span><span className={`status-pill ${statusClass(task.state)}`}>{task.state}</span>
    </li>)}</ul> : !tasks.loading && !tasks.error ? <p className="small-note">No persisted task records.</p> : null}
  </section>;
}

export function DevicesPage() {
  const devices = useResource<DeviceListResponse>("/api/v1/devices");
  const projects = useResource<LogicalProjectListResponse>("/api/v1/logical-projects");
  return <section className="page-stack"><PageHeading eyebrow="Distributed identity" title="Devices">Registration and recent signed activity are not a live connection or proof that a Client Agent is installed.</PageHeading>
    <ResourceMessage loading={devices.loading} error={devices.error} stale={devices.stale} />
    {devices.data?.devices.length ? <div className="device-list">{devices.data.devices.map((device) => <DeviceCard key={device.id} device={device} projects={projects.data?.projects ?? []} onChanged={devices.reload} />)}</div>
      : !devices.loading && !devices.error ? <EmptyState title="No registered devices" detail="Device enrollment and local filesystem consent require a future Client Agent. No device is online." /> : null}
    <ResourceMessage loading={projects.loading} error={projects.error} />
  </section>;
}

function DeviceCard({ device, projects, onChanged }: { device: DeviceMetadataResponse; projects: LogicalProjectResponse[]; onChanged: () => void }) {
  const auth = useAuth();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const changeState = async (action: "authorize" | "revoke") => {
    const prompt = action === "authorize"
      ? `Authorize device ${deviceName(device)}? This enables signed metadata requests but does not install a Client Agent.`
      : `Revoke device ${deviceName(device)} and its active workspace bindings?`;
    if (!window.confirm(prompt) || !auth.csrfToken) return;
    setBusy(true);
    setError(null);
    try {
      await apiRequest(
        `/api/v1/devices/${device.id}${action === "authorize" ? "/authorize" : ""}`,
        { method: action === "authorize" ? "POST" : "DELETE" },
        auth.csrfToken,
      );
      onChanged();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Device state could not be updated.");
    } finally {
      setBusy(false);
    }
  };
  const capabilities = Object.entries(device.capabilities)
    .map(([key, value]) => `${key}: ${typeof value === "string" ? value : JSON.stringify(value)}`)
    .join(" · ") || "No capabilities advertised";
  return <Card className="device-card">
    <div className="device-card-header"><span className="device-icon"><Smartphone size={19} /></span><div><CardTitle>{deviceName(device)}</CardTitle><p className="small-note">Identity {device.id} · key {device.key_fingerprint.slice(0, 16)}…</p></div><span className={`status-pill ${statusClass(device.state)}`}>{device.state}</span></div>
    <dl className="device-details">
      <dt>Authorization</dt><dd>{device.authorized_at ? `Authorized ${dateLabel(device.authorized_at)}` : device.state === "pending" ? "Awaiting operator authorization" : device.state}</dd>
      <dt>Last authenticated activity</dt><dd>{device.last_authenticated_activity_at ? `${dateLabel(device.last_authenticated_activity_at)}${device.recently_active ? " · recent" : ""}` : "No authenticated activity"}</dd>
      <dt>Live connection</dt><dd>Not supported · registered devices are not shown as online</dd>
      <dt>Protocol</dt><dd>Version {device.protocol_version} · credentials expire {dateLabel(device.credential_expires_at)}</dd>
      <dt>Capabilities</dt><dd>{capabilities}</dd>
      <dt>Workspace aliases</dt><dd><DeviceBindings deviceId={device.id} projects={projects} /></dd>
      <dt>Snapshot limits/history</dt><dd>Server-controlled limits apply; per-device quota and history are not exposed by this API.</dd>
    </dl>
    {error && <ErrorNotice message={error} />}
    <div className="device-actions">
      {device.state === "pending" && <Button disabled={busy} onClick={() => void changeState("authorize")}><ShieldCheck size={15} />Authorize device</Button>}
      {device.state === "authorized" && <Button variant="secondary" disabled={busy} onClick={() => void changeState("revoke")}><XCircle size={15} />Revoke</Button>}
      {busy && <span className="small-note">Updating device authorization…</span>}
    </div>
  </Card>;
}

function DeviceBindings({ deviceId, projects }: { deviceId: string; projects: LogicalProjectResponse[] }) {
  if (!projects.length) return <>No logical projects available</>;
  return <span className="binding-aliases">{projects.map((project) => (
    <ProjectBindingAlias key={project.id} project={project} deviceId={deviceId} />
  ))}</span>;
}

function ProjectBindingAlias({ project, deviceId }: { project: LogicalProjectResponse; deviceId: string }) {
  const bindings = useResource<WorkspaceBindingListResponse>(`/api/v1/logical-projects/${project.id}/bindings`);
  const matches = bindings.data?.bindings.filter((binding) => binding.device_id === deviceId) ?? [];
  if (bindings.loading) return null;
  if (bindings.error || !matches.length) return null;
  return <span>{matches.map((binding) => `${project.name} / ${binding.name} (${binding.status})`).join(" · ")}</span>;
}

export function SandboxesPage() {
  const broker = useResource<ExecutionTargetStatusResponse>("/api/v1/execution-targets");
  return <section className="page-stack"><PageHeading eyebrow="Execution boundary" title="Sandboxes">The Broker and runtime administration are intentionally not part of Phase 13D.</PageHeading>
    <ResourceMessage loading={broker.loading} error={broker.error} />
    {broker.data && <Card className="sandbox-status-card">
      <div className="service-summary"><span className={`status-dot ${broker.data.broker_available ? "status-dot-warn" : ""}`} /><div><strong>{broker.data.broker_available ? "Broker metadata reports available" : "Sandbox Broker not installed"}</strong><span>{broker.data.targets.length} execution target records · no execution enabled</span></div></div>
      <StatusMessage tone="neutral" title="Execution disabled">Chat has no terminal or host tool dispatcher. Agent Task execution, container control, and Docker/Podman administration are unavailable.</StatusMessage>
      <div className="future-panels">
        <InfoCard title="Resource status" value="Unavailable" detail="No Broker resource telemetry is configured." />
        <InfoCard title="Task runtime" value="Unavailable" detail="No task is executing in this phase." />
      </div>
    </Card>}
  </section>;
}

export function SettingsPage() {
  const models = useResource<ModelListResponse>("/api/v1/models");
  return <section className="page-stack"><PageHeading eyebrow="Application preferences" title="Settings">Server-side configuration is authoritative. This browser does not store credentials or confidential project content.</PageHeading>
    <Card><div className="settings-line"><SlidersHorizontal size={18} /><div><CardTitle>Model service</CardTitle><p className="muted-copy">{models.error ? "Ollama is currently unavailable." : models.data ? `${models.data.models.length} model(s) available.` : "Checking service…"}</p></div></div></Card>
    <Card><div className="settings-line"><ShieldCheck size={18} /><div><CardTitle>Execution policy</CardTitle><p className="muted-copy">Browser Chat is chat-only. No host tools, sandbox execution, filesystem access, or patch application are enabled.</p></div></div></Card>
    <Card><div className="settings-line"><Waypoints size={18} /><div><CardTitle>Theme</CardTitle><p className="muted-copy">Slate Dark is the fixed Phase 13D theme. No light-mode toggle is provided.</p></div></div></Card>
  </section>;
}

function SectionHeading({ icon, title, to }: { icon: ReactNode; title: string; to?: string }) {
  return <div className="section-heading"><h2>{icon}{title}</h2>{to && <Link to={to} className="text-link">View all <ArrowUpRight size={14} /></Link>}</div>;
}

function PageHeading({ eyebrow, title, children }: { eyebrow: string; title: string; children: ReactNode }) {
  return <header className="page-heading"><span className="eyebrow">{eyebrow}</span><h1>{title}</h1><p className="muted-copy">{children}</p></header>;
}

function ResourceMessage({ loading, error, stale }: { loading?: boolean; error?: string | null; stale?: boolean }) {
  if (loading) return <p className="resource-hint" role="status">Loading service data…</p>;
  if (error) return <p className="resource-error" role="alert">{stale ? "Showing saved data; refresh failed. " : ""}{error}</p>;
  return null;
}

function EmptyState({ title, detail, action }: { title: string; detail: string; action?: ReactNode }) {
  return <div className="empty-state"><div className="empty-icon"><FolderKanban size={18} /></div><h3>{title}</h3><p>{detail}</p>{action}</div>;
}

function StatusMessage({ tone, title, children }: { tone: "neutral" | "warning" | "error"; title: string; children: ReactNode }) {
  return <div className={`status-message tone-${tone}`} role={tone === "error" ? "alert" : "status"}><strong>{title}</strong><p>{children}</p></div>;
}

function ErrorNotice({ message }: { message: string }) {
  return <p className="error-message" role="alert">{message}</p>;
}

function ErrorPage({ title, message }: { title: string; message: string }) {
  return <section className="page-stack"><PageHeading eyebrow="Service response" title={title}>{message}</PageHeading><Button asChild variant="secondary"><Link to="/projects">Return to projects</Link></Button></section>;
}

function statusClass(state: string) {
  if (["complete", "completed", "authorized", "available", "idle"].includes(state)) return "status-good";
  if (["error", "failed", "revoked", "cancelled"].includes(state)) return "status-bad";
  if (["pending", "running", "queued", "claimed", "interrupted", "not_enabled"].includes(state)) return "status-warn";
  return "status-neutral";
}

function dateLabel(value: number | string | null) {
  if (value === null) return "Unavailable";
  const date = typeof value === "number" ? new Date(value * 1000) : new Date(value);
  return Number.isNaN(date.getTime()) ? "Unavailable" : date.toLocaleString();
}

function activityLabel(type: ProjectActivityEvent["type"], payload: ProjectActivityEvent["payload"]) {
  switch (type) {
    case "project_created":
      return `Project created · ${String(payload.project_name ?? "logical project")}`;
    case "workspace_binding_created":
      return `Workspace binding created · ${String(payload.name ?? "binding")}`;
    case "workspace_binding_revoked":
      return `Workspace binding revoked · ${String(payload.name ?? "binding")}`;
    case "device_revoked":
      return `Device authorization revoked · ${String(payload.device_id ?? "device")}`;
    case "snapshot_committed":
      return `Snapshot committed · ${String(payload.snapshot_id ?? "snapshot")}`;
    case "snapshot_expired":
      return `Snapshot expired · ${String(payload.snapshot_id ?? "snapshot")}`;
    case "project_snapshot":
      return "Project state synchronized";
    case "resynchronization_required":
      return "Activity gap detected · refreshing saved state";
  }
}

function formatBytes(value: number) {
  if (value < 1024) return `${value} B`;
  if (value < 1024 ** 2) return `${(value / 1024).toFixed(1)} KB`;
  return `${(value / 1024 ** 2).toFixed(1)} MB`;
}

function deviceName(device: DeviceMetadataResponse) {
  return `Device ${device.id.slice(0, 8)}`;
}
