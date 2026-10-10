import { chromium, expect, test } from "@playwright/test";
import { existsSync } from "node:fs";

const projectId = "a".repeat(32);
const conversationId = "b".repeat(32);
const now = "2026-05-11T12:00:00Z";

test.describe("Phase 13D browser acceptance", () => {
test.skip(!existsSync(chromium.executablePath()), "Playwright Chromium is not installed on this host.");

test("browser command center, chat, project status, and protected navigation", async ({ page }) => {
  let authenticated = false;
  let conversation: Record<string, unknown> | null = null;

  await page.addInitScript(() => {
    type Socket = {
      url: string;
      onopen: (() => void) | null;
      onmessage: ((event: { data: string }) => void) | null;
      onclose: ((event: { code: number }) => void) | null;
      close: () => void;
    };
    const sockets: Socket[] = [];
    class MockWebSocket {
      static OPEN = 1;
      readyState = 0;
      onopen: (() => void) | null = null;
      onmessage: ((event: { data: string }) => void) | null = null;
      onclose: ((event: { code: number }) => void) | null = null;
      constructor(readonly url: string) {
        sockets.push(this);
        setTimeout(() => {
          this.readyState = 1;
          this.onopen?.();
          const match = this.url.match(/\/chat\/([a-f0-9]{32})/);
          if (match) this.onmessage?.({ data: JSON.stringify({
            schema_version: 1, event_id: 0, conversation_id: match[1],
            type: "session_snapshot", payload: {},
          }) });
        }, 0);
      }
      close() { this.readyState = 3; }
    }
    Object.assign(window, {
      WebSocket: MockWebSocket,
      __emitChatEvent: (id: string, event: Record<string, unknown>) => {
        sockets.filter((socket) => socket.url.includes(`/chat/${id}`))
          .forEach((socket) => socket.onmessage?.({ data: JSON.stringify(event) }));
      },
      __disconnectChat: () => sockets.forEach((socket) => socket.onclose?.({ code: 1006 })),
    });
  });

  await page.route("**/api/v1/**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname;
    const json = (body: unknown, status = 200) => route.fulfill({
      status, contentType: "application/json", body: JSON.stringify(body),
    });
    if (path.endsWith("/auth/session")) {
      return json({ authenticated }, authenticated ? 200 : 401);
    }
    if (path.endsWith("/auth/login") && request.method() === "POST") {
      authenticated = true;
      return json({ authenticated: true, csrf_token: "test-csrf", expires_at: now });
    }
    if (path.endsWith("/auth/csrf")) return json({ csrf_token: "test-csrf" });
    if (path.endsWith("/auth/logout")) {
      authenticated = false;
      return json({ authenticated: false });
    }
    if (path === "/api/v1/models") return json({ models: [{ name: "test-model" }] });
    if (path.startsWith("/api/v1/chat/sessions") && request.method() === "POST" && path.endsWith("/sessions")) {
      conversation = {
        schema_version: 1, id: conversationId, project_id: null, title: "New conversation",
        model: "test-model", state: "idle", created_at: now, updated_at: now, messages: [],
      };
      return json(conversation, 201);
    }
    if (path === `/api/v1/chat/sessions/${conversationId}/turns`) {
      const body = request.postDataJSON() as { prompt: string; model: string };
      conversation = {
        ...(conversation ?? {}), title: body.prompt, model: body.model, state: "running",
        messages: [{ role: "user", content: body.prompt, thinking: "", status: "complete", created_at: now }],
      };
      const event = (eventId: number, type: string, payload: Record<string, unknown> = {}) => ({
        schema_version: 1, event_id: eventId, conversation_id: conversationId, type, payload,
      });
      setTimeout(() => {
        void page.evaluate(({ id, started, delta }) => {
          const target = window as typeof window & {
            __emitChatEvent: (key: string, value: Record<string, unknown>) => void;
          };
          target.__emitChatEvent(id, started);
          target.__emitChatEvent(id, delta);
        }, {
          id: conversationId,
          started: event(1, "turn_started"),
          delta: event(2, "content_delta", { text: "A streamed answer." }),
        });
      }, 40);
      return json({ conversation_id: conversationId, model: body.model, state: "running" }, 202);
    }
    if (path === `/api/v1/chat/sessions/${conversationId}/cancel`) {
      conversation = { ...(conversation ?? {}), state: "cancelled" };
      await page.evaluate(({ id, event }) => {
        const target = window as typeof window & {
          __emitChatEvent: (key: string, value: Record<string, unknown>) => void;
        };
        target.__emitChatEvent(id, event);
      }, {
        id: conversationId,
        event: {
          schema_version: 1, event_id: 3, conversation_id: conversationId,
          type: "turn_cancelled", payload: {},
        },
      });
      return json({ conversation_id: conversationId, state: "cancelled", cancelled: true });
    }
    if (path === `/api/v1/chat/sessions/${conversationId}`) {
      return conversation ? json(conversation) : json({ detail: "Not found" }, 404);
    }
    if (path === "/api/v1/chat/sessions" || path === "/api/v1/chat/sessions") {
      return json({ conversations: conversation ? [conversation] : [] });
    }
    if (path === "/api/v1/logical-projects") {
      return json({ projects: [{
        id: projectId, schema_version: 1, name: "Acceptance project", status: "active", created_at: 1,
      }] });
    }
    if (path === `/api/v1/logical-projects/${projectId}`) {
      return json({ id: projectId, schema_version: 1, name: "Acceptance project", status: "active", created_at: 1 });
    }
    if (path === "/api/v1/projects") return json({ projects: [] });
    if (path === `/api/v1/logical-projects/${projectId}/snapshots`) return json({ snapshots: [] });
    if (path === `/api/v1/logical-projects/${projectId}/bindings`) return json({ bindings: [] });
    if (path === `/api/v1/logical-projects/${projectId}/activity`) {
      return json({ schema_version: 1, project_id: projectId, cursor: 0, events: [] });
    }
    if (path === `/api/v1/logical-projects/${projectId}/tasks`) return json({ tasks: [] });
    if (path === "/api/v1/devices") return json({ devices: [] });
    if (path === "/api/v1/execution-targets") {
      return json({ schema_version: 1, targets: [], execution_available: false, broker_available: false });
    }
    if (path === "/api/v1/chat/sessions?limit=12" || path === "/api/v1/chat/sessions") {
      return json({ conversations: conversation ? [conversation] : [] });
    }
    return json({});
  });

  await page.goto("/");
  await page.getByLabel("password").fill("test password");
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page.getByRole("heading", { name: "Project Command Center" })).toBeVisible();
  await expect(page.getByText("Acceptance project")).toBeVisible();

  await page.getByRole("link", { name: "Chat", exact: true }).click();
  await page.getByLabel("Model").selectOption("test-model");
  await page.getByLabel("Message").fill("hello");
  await page.getByRole("button", { name: "Send" }).click();
  await expect(page.getByText("A streamed answer.")).toBeVisible();
  await page.getByRole("button", { name: "Stop generation" }).click();
  await expect(page.getByText("Generation stopped. Partial response was saved.")).toBeVisible();

  await page.getByRole("link", { name: "Projects", exact: true }).click();
  await page.getByRole("button", { name: "Open Acceptance project Workbench" }).click();
  await expect(page.getByRole("heading", { name: "Activity" })).toBeVisible();
  await page.getByRole("link", { name: "Devices", exact: true }).click();
  await expect(page.getByText("No registered devices")).toBeVisible();
  await page.getByRole("link", { name: "Sandboxes", exact: true }).click();
  await expect(page.getByText("Sandbox Broker not installed")).toBeVisible();

  await page.getByRole("button", { name: "Sign out" }).click();
  await page.goto("/devices");
  await expect(page.getByRole("heading", { name: "Sign in" })).toBeVisible();
});

test("invalid navigation remains inside the authenticated application", async ({ page }) => {
  await page.route("**/api/v1/auth/session", (route) => route.fulfill({
    status: 401, contentType: "application/json", body: JSON.stringify({ detail: "Unauthorized" }),
  }));
  await page.goto("/not-a-real-page");
  await expect(page.getByRole("heading", { name: "Sign in" })).toBeVisible();
});

test("login errors are visible and Ollama failure leaves navigation available", async ({ page }) => {
  let authenticated = false;
  await page.route("**/api/v1/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    const json = (body: unknown, status = 200) => route.fulfill({
      status, contentType: "application/json", body: JSON.stringify(body),
    });
    if (path.endsWith("/auth/session")) return json({ authenticated }, authenticated ? 200 : 401);
    if (path.endsWith("/auth/login")) {
      if (route.request().postDataJSON()?.password !== "correct password") {
        return json({ error: { code: "authentication_failed", message: "Sign in failed." } }, 401);
      }
      authenticated = true;
      return json({ authenticated: true, csrf_token: "test-csrf", expires_at: now });
    }
    if (path === "/api/v1/models") return json({ error: { code: "provider_unavailable", message: "Unavailable" } }, 503);
    if (path === "/api/v1/logical-projects" || path === "/api/v1/projects") return json({ projects: [] });
    if (path === "/api/v1/chat/sessions") return json({ conversations: [] });
    if (path === "/api/v1/devices") return json({ devices: [] });
    if (path === "/api/v1/execution-targets") {
      return json({ schema_version: 1, targets: [], execution_available: false, broker_available: false });
    }
    return json({});
  });

  await page.goto("/");
  await page.getByLabel("password").fill("incorrect password");
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page.getByRole("alert")).toContainText("Sign in failed");
  await page.getByLabel("password").fill("correct password");
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page.getByRole("heading", { name: "Project Command Center" })).toBeVisible();
  await expect(page.getByText("Ollama unavailable")).toBeVisible();
  await page.getByRole("link", { name: "Projects", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Projects" })).toBeVisible();
});
});
