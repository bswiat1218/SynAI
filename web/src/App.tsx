import * as Dialog from "@radix-ui/react-dialog";
import { Menu, PanelsTopLeft, X } from "lucide-react";
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
  useParams,
} from "react-router-dom";
import { AuthProvider, useAuth } from "./auth/AuthContext";
import { Button } from "./components/ui/button";
import { Card, CardTitle } from "./components/ui/card";
import { Separator } from "./components/ui/separator";

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
          <Route path="/workbench/:projectId" element={<Workbench />} />
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
  if (auth.error) {
    return <ErrorState message={auth.error} />;
  }
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
    } catch {
      setLogoutError("Could not end the session. Try again.");
    }
  };
  return (
    <div className="application-shell">
      <header className="topbar">
        <div className="brand">
          <PanelsTopLeft aria-hidden="true" size={20} />
          <span>SynAI</span>
        </div>
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
          <span className="session-label">Single-user session</span>
          <Button variant="ghost" onClick={() => void logout()}>Sign out</Button>
        </div>
      </header>
      <div className="shell-body">
        <aside className="sidebar" aria-label="Primary navigation">
          <Navigation />
        </aside>
        <main className="page-content">
          {logoutError && <ErrorNotice message={logoutError} />}
          <Outlet />
        </main>
      </div>
    </div>
  );
}

function Navigation({ onNavigate }: { onNavigate?: () => void }) {
  return (
    <nav className="navigation" aria-label="Primary navigation">
      <NavLink to="/" end onClick={onNavigate}>Project Command Center</NavLink>
      <NavLink to="/workbench/demo" onClick={onNavigate}>Developer Workbench</NavLink>
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
      // The provider stores a bounded public error for rendering.
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
        <p className="muted-copy text-xs">SynAI web mode does not execute tools or modify projects.</p>
      </Card>
    </main>
  );
}

function CommandCenter() {
  return (
    <section className="content-stack">
      <div>
        <p className="eyebrow">Workspace overview</p>
        <h1>Project Command Center</h1>
        <p className="muted-copy">A read-only foundation for your registered SynAI workspaces.</p>
      </div>
      <div className="content-grid">
        <Card>
          <p className="eyebrow">Projects</p>
          <CardTitle>No project selected</CardTitle>
          <p className="muted-copy">Projects become available after an operator-configured workspace is registered.</p>
        </Card>
        <Card>
          <p className="eyebrow">Activity</p>
          <CardTitle>Ready for your workspace</CardTitle>
          <p className="muted-copy">Chat and Agent Task execution are disabled in this foundation phase.</p>
        </Card>
      </div>
      <Button asChild variant="secondary"><Link to="/workbench/demo">Open Workbench shell</Link></Button>
    </section>
  );
}

function Workbench() {
  const { projectId } = useParams();
  return (
    <section className="content-stack">
      <div>
        <p className="eyebrow">Developer Workbench</p>
        <h1>Workbench shell</h1>
        <p className="muted-copy">Project {projectId ?? "not selected"} · read-only foundation</p>
      </div>
      <div className="workbench-grid">
        <Card className="workbench-pane">
          <p className="eyebrow">Project</p>
          <CardTitle>Workspace context</CardTitle>
          <p className="muted-copy">Registered project metadata will appear here.</p>
        </Card>
        <Card className="workbench-pane workbench-main">
          <p className="eyebrow">Conversation</p>
          <CardTitle>Execution is disabled</CardTitle>
          <p className="muted-copy">No chat, streaming, tools, or task execution is exposed in Phase 13B.</p>
        </Card>
        <aside className="details-pane" aria-label="Resizable details pane">
          <Card className="workbench-pane">
            <p className="eyebrow">Details</p>
            <CardTitle>Project details</CardTitle>
            <p className="muted-copy">A future workbench panel.</p>
          </Card>
        </aside>
      </div>
    </section>
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
        <ErrorNotice message={message} />
        <Button variant="secondary" onClick={() => window.location.reload()}>Try again</Button>
      </Card>
    </main>
  );
}

function ErrorNotice({ message }: { message: string }) {
  return <p className="error-message" role="alert">{message}</p>;
}
