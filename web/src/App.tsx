import * as Dialog from "@radix-ui/react-dialog";
import {
  Bot,
  FolderKanban,
  LayoutDashboard,
  Menu,
  MessageSquare,
  PanelsTopLeft,
  Settings2,
  Smartphone,
  SquareTerminal,
  X,
} from "lucide-react";
import { useState, type FormEvent } from "react";
import {
  BrowserRouter,
  Link,
  Navigate,
  NavLink,
  Outlet,
  Route,
  Routes,
  useLocation,
} from "react-router-dom";
import { AuthProvider, useAuth } from "./auth/AuthContext";
import { Button } from "./components/ui/button";
import { Card, CardTitle } from "./components/ui/card";
import { Separator } from "./components/ui/separator";
import {
  AgentTasksPage,
  ChatPage,
  clearChatDrafts,
  CommandCenter,
  DevicesPage,
  ProjectsPage,
  SandboxesPage,
  SettingsPage,
  WorkbenchPage,
} from "./pages";

export function App() {
  return (
    <AuthProvider>
      <BrowserRouter>
        <AppRoutes />
      </BrowserRouter>
    </AuthProvider>
  );
}

export function AppRoutes() {
  const auth = useAuth();
  return (
    <Routes>
      <Route
        path="/login"
        element={auth.authenticated ? <Navigate to="/" replace /> : <LoginPage />}
      />
      <Route element={<RequireAuthentication />}>
        <Route element={<ApplicationShell />}>
          <Route path="/" element={<CommandCenter />} />
          <Route path="/chat" element={<ChatPage />} />
          <Route path="/chat/:conversationId" element={<ChatPage />} />
          <Route path="/projects" element={<ProjectsPage />} />
          <Route path="/projects/:projectId/chat" element={<ChatPage />} />
          <Route path="/projects/:projectId/chat/:conversationId" element={<ChatPage />} />
          <Route path="/projects/:projectId/agent-tasks" element={<AgentTasksPage />} />
          <Route path="/workbench/:projectId/chat" element={<WorkbenchPage />} />
          <Route path="/workbench/:projectId/chat/:conversationId" element={<WorkbenchPage />} />
          <Route path="/workbench/:projectId" element={<WorkbenchPage />} />
          <Route path="/agent-tasks" element={<AgentTasksPage />} />
          <Route path="/devices" element={<DevicesPage />} />
          <Route path="/sandboxes" element={<SandboxesPage />} />
          <Route path="/settings" element={<SettingsPage />} />
          <Route path="*" element={<NotFound />} />
        </Route>
      </Route>
    </Routes>
  );
}

function RequireAuthentication() {
  const auth = useAuth();
  const location = useLocation();
  if (auth.loading) {
    return <main className="center-state" aria-live="polite">Checking your session…</main>;
  }
  if (auth.error) return <ErrorState message={auth.error} />;
  if (!auth.authenticated) {
    return <Navigate to="/login" replace state={{ from: location.pathname }} />;
  }
  return <Outlet />;
}

function ApplicationShell() {
  const auth = useAuth();
  const [mobileNavigationOpen, setMobileNavigationOpen] = useState(false);
  const [logoutError, setLogoutError] = useState<string | null>(null);
  const logout = async () => {
    try {
      await auth.logout();
      clearChatDrafts();
    } catch {
      setLogoutError("Could not end the session. Try again.");
    }
  };
  return (
    <div className="application-shell">
      <header className="topbar">
        <Link className="brand" to="/" aria-label="SynAI Command Center">
          <PanelsTopLeft aria-hidden="true" size={20} />
          <span>SynAI</span>
          <span className="brand-version">2.0</span>
        </Link>
        <Dialog.Root open={mobileNavigationOpen} onOpenChange={setMobileNavigationOpen}>
          <Dialog.Trigger asChild>
            <Button className="mobile-menu-trigger" variant="secondary" aria-label="Open navigation">
              <Menu aria-hidden="true" size={18} />
            </Button>
          </Dialog.Trigger>
          <Dialog.Portal>
            <Dialog.Overlay className="mobile-nav-overlay" />
            <Dialog.Content className="mobile-nav-panel">
              <div className="mobile-nav-heading">
                <Dialog.Title className="text-sm font-semibold">Navigation</Dialog.Title>
                <Dialog.Close asChild>
                  <Button variant="ghost" aria-label="Close navigation"><X size={18} /></Button>
                </Dialog.Close>
              </div>
              <Navigation onNavigate={() => setMobileNavigationOpen(false)} />
            </Dialog.Content>
          </Dialog.Portal>
        </Dialog.Root>
        <div className="topbar-actions">
          <span className="session-label"><span className="status-dot status-dot-good" />Authenticated browser session</span>
          <Button variant="ghost" onClick={() => void logout()}>Sign out</Button>
        </div>
      </header>
      <div className="shell-body">
        <aside className="sidebar" aria-label="Application navigation">
          <Navigation />
          <div className="sidebar-footer">
            <span className="eyebrow">Phase 13D</span>
            <span>Chat only · execution disabled</span>
          </div>
        </aside>
        <main className="page-content">
          {logoutError && <p className="error-message" role="alert">{logoutError}</p>}
          <Outlet />
        </main>
      </div>
    </div>
  );
}

function Navigation({ onNavigate }: { onNavigate?: () => void }) {
  const entries = [
    { to: "/", label: "Command Center", icon: LayoutDashboard, end: true },
    { to: "/chat", label: "Chat", icon: MessageSquare },
    { to: "/projects", label: "Projects", icon: FolderKanban },
    { to: "/agent-tasks", label: "Agent Tasks", icon: Bot },
    { to: "/devices", label: "Devices", icon: Smartphone },
    { to: "/sandboxes", label: "Sandboxes", icon: SquareTerminal },
    { to: "/settings", label: "Settings", icon: Settings2 },
  ];
  return (
    <nav className="navigation" aria-label="Primary navigation">
      <span className="nav-section-label">Workspace</span>
      {entries.map(({ to, label, icon: Icon, end }) => (
        <NavLink key={to} to={to} end={end} onClick={onNavigate}>
          <Icon aria-hidden="true" size={17} />
          <span>{label}</span>
          {label === "Agent Tasks" && <span className="nav-badge">disabled</span>}
        </NavLink>
      ))}
    </nav>
  );
}

function LoginPage() {
  const auth = useAuth();
  const [password, setPassword] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setSubmitting(true);
    try {
      await auth.login(password);
    } catch {
      // AuthContext provides a bounded public error message.
    } finally {
      setSubmitting(false);
    }
  };
  if (auth.loading) {
    return <main className="center-state" aria-live="polite">Checking your session…</main>;
  }
  return (
    <main className="login-page">
      <Card className="login-card">
        <div className="brand login-brand"><PanelsTopLeft aria-hidden="true" size={20} /><span>SynAI</span></div>
        <span className="eyebrow">Secure browser access</span>
        <CardTitle>Sign in</CardTitle>
        <p className="muted-copy">Use the credential configured by your SynAI operator.</p>
        <form className="login-form" onSubmit={(event) => void submit(event)}>
          <label htmlFor="password">Password</label>
          <input
            id="password"
            name="password"
            type="password"
            autoComplete="current-password"
            required
            maxLength={1024}
            value={password}
            onChange={(event) => setPassword(event.target.value)}
          />
          {auth.error && <p role="alert" className="error-message">{auth.error}</p>}
          <Button type="submit" disabled={submitting || password.length === 0}>
            {submitting ? "Signing in…" : "Sign in"}
          </Button>
        </form>
        <Separator />
        <p className="muted-copy text-xs">Browser chat has no tools, filesystem access, or task execution authority.</p>
      </Card>
    </main>
  );
}

function NotFound() {
  return (
    <section className="center-state">
      <Card>
        <p className="eyebrow">404</p>
        <CardTitle>Page not found</CardTitle>
        <p className="muted-copy">That SynAI page does not exist.</p>
        <Button asChild variant="secondary"><Link to="/">Return to Command Center</Link></Button>
      </Card>
    </section>
  );
}

function ErrorState({ message }: { message: string }) {
  return (
    <main className="center-state">
      <Card>
        <CardTitle>Service unavailable</CardTitle>
        <p className="error-message" role="alert">{message}</p>
        <Button variant="secondary" onClick={() => window.location.reload()}>Try again</Button>
      </Card>
    </main>
  );
}
