import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";
import { AppRoutes } from "./App";
import { AuthProvider } from "./auth/AuthContext";
import { Button } from "./components/ui/button";
import { MessageContent } from "./components/MessageContent";

function jsonResponse(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function renderRoute(path: string) {
  return render(
    <AuthProvider>
      <MemoryRouter initialEntries={[path]}>
        <AppRoutes />
      </MemoryRouter>
    </AuthProvider>,
  );
}

function authenticatedApi() {
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    if (url.endsWith("/api/v1/auth/session")) {
      return jsonResponse({ authenticated: true, expires_at: 1_900_000_000 });
    }
    if (url.endsWith("/api/v1/auth/csrf")) {
      return jsonResponse({ csrf_token: "a-long-test-csrf-token-value-12345" });
    }
    if (url.endsWith("/api/v1/logical-projects")) return jsonResponse({ projects: [] });
    if (url.endsWith("/api/v1/projects")) return jsonResponse({ projects: [] });
    if (url.endsWith("/api/v1/devices")) return jsonResponse({ devices: [] });
    if (url.endsWith("/api/v1/models")) return jsonResponse({ models: [] });
    if (url.endsWith("/api/v1/execution-targets")) return jsonResponse({ available: false });
    if (/\/api\/v1\/logical-projects\/[^/]+\/bindings$/.test(url)) return jsonResponse({ bindings: [] });
    if (/\/api\/v1\/logical-projects\/[^/]+\/snapshots$/.test(url)) return jsonResponse({ snapshots: [] });
    if (/\/api\/v1\/logical-projects\/[^/]+\/tasks$/.test(url)) return jsonResponse({ tasks: [], execution_available: false });
    if (/\/api\/v1\/logical-projects\/[^/]+$/.test(url)) {
      return jsonResponse({ id: "00000000000000000000000000000000", name: "Test project", created_at: 1_700_000_000 });
    }
    return jsonResponse({ conversations: [] });
  }));
}

describe("SynAI web foundation", () => {
  it("guards project routes and presents the login route", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(
      jsonResponse({ error: { code: "unauthenticated", message: "Authentication required.", request_id: "abc" } }, 401),
    ));
    renderRoute("/workbench/00000000000000000000000000000000");
    expect(await screen.findByRole("heading", { name: "Sign in" })).toBeInTheDocument();
    expect(screen.getByLabelText("Password")).toHaveAttribute("type", "password");
  });

  it("renders a bounded API login error without disclosing request data", async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(jsonResponse({ error: { code: "unauthenticated", message: "Invalid credentials.", request_id: "abc" } }, 401))
      .mockResolvedValueOnce(jsonResponse({ error: { code: "unauthenticated", message: "Invalid credentials.", request_id: "abc" } }, 401));
    vi.stubGlobal("fetch", fetchMock);
    renderRoute("/login");
    await screen.findByRole("heading", { name: "Sign in" });
    await userEvent.type(screen.getByLabelText("Password"), "wrong secret");
    await userEvent.click(screen.getByRole("button", { name: "Sign in" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Invalid credentials.");
    expect(screen.getByRole("alert")).not.toHaveTextContent("wrong secret");
  });

  it("loads the Command Center only after session and CSRF initialization", async () => {
    vi.stubGlobal("fetch", vi.fn()
      .mockResolvedValueOnce(jsonResponse({ authenticated: true, expires_at: 1_900_000_000 }))
      .mockResolvedValueOnce(jsonResponse({ csrf_token: "a-long-test-csrf-token-value-12345" })));
    renderRoute("/");
    expect(await screen.findByRole("heading", { name: "Project Command Center" })).toBeInTheDocument();
    expect(screen.getByRole("navigation", { name: "Primary navigation" })).toBeInTheDocument();
  });

  it("renders the Workbench shell and a resizable details region", async () => {
    authenticatedApi();
    renderRoute("/workbench/00000000000000000000000000000000");
    expect((await screen.findAllByRole("heading", { name: "Test project" })).length).toBeGreaterThan(0);
    expect(screen.getByRole("complementary", { name: "Project details inspector" })).toHaveClass("details-pane");
    expect(screen.getByText(/does not access a source workspace or execute tasks/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /Open project chat/ })).toHaveAttribute(
      "href", "/projects/00000000000000000000000000000000/chat",
    );
    await userEvent.click(screen.getByRole("button", { name: /^Chat$/ }));
    expect(await screen.findByRole("textbox", { name: "Message" })).toBeInTheDocument();
    expect(screen.getByText(/No source files or client workspace are provided to the model/)).toBeInTheDocument();
  });

  it("provides keyboard focus styling and uses Slate Dark tokens", async () => {
    render(<Button variant="secondary">Keyboard action</Button>);
    await userEvent.tab();
    expect(screen.getByRole("button", { name: "Keyboard action" })).toHaveFocus();
    expect(screen.getByRole("button", { name: "Keyboard action" }).className).toContain("focus-visible:ring-sky-300");
    expect(document.documentElement.dataset.theme).toBe("slate-dark");
    await waitFor(() => {
      expect(document.documentElement).toHaveClass("dark");
    });
  });

  it("opens keyboard-operable collapsed navigation at narrow layouts", async () => {
    authenticatedApi();
    renderRoute("/");
    await screen.findByRole("heading", { name: "Project Command Center" });
    await userEvent.click(screen.getByRole("button", { name: "Open navigation" }));
    expect(await screen.findByRole("dialog", { name: "Navigation" })).toBeInTheDocument();
    expect(screen.getAllByRole("link", { name: "Command Center" }).length).toBeGreaterThan(0);
    fireEvent.keyDown(screen.getByRole("dialog", { name: "Navigation" }), { key: "Escape" });
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "Navigation" })).not.toBeInTheDocument());
  });

  it("creates a general browser conversation without a project or execution controls", async () => {
    const conversationId = "a".repeat(32);
    const sentBodies: Array<{ url: string; body: unknown }> = [];
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith("/api/v1/auth/session")) return jsonResponse({ authenticated: true, expires_at: 1_900_000_000 });
      if (url.endsWith("/api/v1/auth/csrf")) return jsonResponse({ csrf_token: "a-long-test-csrf-token-value-12345" });
      if (url.endsWith("/api/v1/models")) return jsonResponse({ models: [{ name: "test-model" }] });
      if (url.endsWith("/api/v1/chat/sessions?limit=100")) return jsonResponse({ conversations: [] });
      if (url.endsWith("/api/v1/chat/sessions") && init?.method === "POST") {
        const body = JSON.parse(String(init.body)) as unknown;
        sentBodies.push({ url, body });
        return jsonResponse({
          schema_version: 1, id: conversationId, project_id: null, title: "New conversation",
          model: "test-model", state: "idle", created_at: "2026-01-01T00:00:00Z",
          updated_at: "2026-01-01T00:00:00Z", messages: [],
        }, 201);
      }
      if (url.endsWith(`/api/v1/chat/sessions/${conversationId}/turns`)) {
        sentBodies.push({ url, body: JSON.parse(String(init?.body)) as unknown });
        return jsonResponse({ conversation_id: conversationId, model: "test-model", state: "running" }, 202);
      }
      if (url.endsWith(`/api/v1/chat/sessions/${conversationId}`)) {
        return jsonResponse({
          schema_version: 1, id: conversationId, project_id: null, title: "Hello from browser",
          model: "test-model", state: "running", created_at: "2026-01-01T00:00:00Z",
          updated_at: "2026-01-01T00:00:00Z", messages: [],
        });
      }
      return jsonResponse({ conversations: [] });
    });
    vi.stubGlobal("fetch", fetchMock);
    renderRoute("/chat");
    const composer = await screen.findByRole("textbox", { name: "Message" });
    await userEvent.type(composer, "Hello from browser");
    await userEvent.click(screen.getByRole("button", { name: "Send" }));
    await waitFor(() => expect(sentBodies).toHaveLength(2));
    expect(sentBodies[0].body).toEqual({ model: "test-model", project_id: null });
    expect(sentBodies[1].body).toEqual({ prompt: "Hello from browser", model: "test-model" });
    expect(screen.getByText(/no tools, terminal, filesystem, or patch authority/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Run|Approve|Terminal/ })).not.toBeInTheDocument();
  });

  it("renders model-provided HTML as inert text", () => {
    render(<MessageContent content={"<img src=x onerror=alert(1)>"} />);
    expect(screen.getByText("<img src=x onerror=alert(1)>")).toBeInTheDocument();
    expect(document.querySelector("img")).not.toBeInTheDocument();
  });

  it("returns to sign-in when an authenticated request discovers an expired session", async () => {
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith("/api/v1/auth/session")) return jsonResponse({ authenticated: true, expires_at: 1_900_000_000 });
      if (url.endsWith("/api/v1/auth/csrf")) return jsonResponse({ csrf_token: "a-long-test-csrf-token-value-12345" });
      if (url.endsWith("/api/v1/models")) {
        return jsonResponse({ error: { code: "unauthenticated", message: "Authentication required.", request_id: "abc" } }, 401);
      }
      return jsonResponse({ conversations: [] });
    }));
    renderRoute("/chat");
    expect(await screen.findByRole("heading", { name: "Sign in" })).toBeInTheDocument();
  });
});
