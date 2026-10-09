import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";
import { AppRoutes } from "./App";
import { AuthProvider } from "./auth/AuthContext";
import { Button } from "./components/ui/button";

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

describe("SynAI web foundation", () => {
  it("guards project routes and presents the login route", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(
      jsonResponse({ error: { code: "unauthenticated", message: "Authentication required.", request_id: "abc" } }, 401),
    ));
    renderRoute("/workbench/demo");
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
    vi.stubGlobal("fetch", vi.fn()
      .mockResolvedValueOnce(jsonResponse({ authenticated: true, expires_at: 1_900_000_000 }))
      .mockResolvedValueOnce(jsonResponse({ csrf_token: "a-long-test-csrf-token-value-12345" })));
    renderRoute("/workbench/project-a");
    expect(await screen.findByRole("heading", { name: "Workbench shell" })).toBeInTheDocument();
    expect(screen.getByRole("complementary", { name: "Resizable details pane" })).toHaveClass("details-pane");
    expect(screen.getByText(/No chat, streaming, tools, or task execution/)).toBeInTheDocument();
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
    vi.stubGlobal("fetch", vi.fn()
      .mockResolvedValueOnce(jsonResponse({ authenticated: true, expires_at: 1_900_000_000 }))
      .mockResolvedValueOnce(jsonResponse({ csrf_token: "a-long-test-csrf-token-value-12345" })));
    renderRoute("/");
    await screen.findByRole("heading", { name: "Project Command Center" });
    await userEvent.click(screen.getByRole("button", { name: "Open navigation" }));
    expect(await screen.findByRole("dialog", { name: "Navigation" })).toBeInTheDocument();
    expect(screen.getAllByRole("link", { name: "Developer Workbench" }).length).toBeGreaterThan(0);
    fireEvent.keyDown(screen.getByRole("dialog", { name: "Navigation" }), { key: "Escape" });
  });
});
