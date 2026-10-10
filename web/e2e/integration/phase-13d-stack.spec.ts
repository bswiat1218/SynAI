import AxeBuilder from "@axe-core/playwright";
import { expect, test, type WebSocketRoute } from "@playwright/test";
import { existsSync, readFileSync, writeFileSync } from "node:fs";
import { chromium } from "@playwright/test";

const projectName = `Browser acceptance ${Date.now()}`;
const password = "disposable-browser-acceptance-password";
const providerState = process.env.SYNAI_E2E_PROVIDER_STATE;
const providerCalls = process.env.SYNAI_E2E_PROVIDER_CALLS;
const appOrigin = "http://127.0.0.1:4179";

test.skip(!existsSync(chromium.executablePath()), "Playwright Chromium is not installed.");

function setProviderUnavailable(unavailable: boolean) {
  if (!providerState) throw new Error("E2E provider-state path is not configured.");
  writeFileSync(providerState, JSON.stringify({ unavailable }), { mode: 0o600 });
}

function providerCallCount() {
  if (!providerCalls) throw new Error("E2E provider-call path is not configured.");
  return readFileSync(providerCalls, "utf-8").trim().split("\n").filter(Boolean).length;
}

async function runAxe(page: import("@playwright/test").Page) {
  const results = await new AxeBuilder({ page })
    .withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"])
    .analyze();
  expect(results.violations.map(({ id, impact, nodes }) => ({
    id,
    impact,
    elements: nodes.map((node) => ({
      target: node.target,
      html: node.html,
      summary: node.failureSummary,
    })),
  }))).toEqual([]);
}

test("real FastAPI, React, cookie auth, streamed chat, recovery and cancellation", async ({ page }) => {
  test.setTimeout(120_000);
  await page.setViewportSize({ width: 1440, height: 960 });
  await page.addInitScript(() => {
    const NativeWebSocket = window.WebSocket;
    const sockets: WebSocket[] = [];
    class TrackedWebSocket extends NativeWebSocket {
      constructor(url: string | URL, protocols?: string | string[]) {
        super(url, protocols);
        sockets.push(this);
      }
    }
    Object.defineProperty(window, "WebSocket", { value: TrackedWebSocket });
    Object.defineProperty(window, "__synaiSockets", { value: sockets });
  });
  await page.goto("/");
  await expect(page.getByRole("heading", { name: "Sign in" })).toBeVisible();
  await page.getByLabel("password").focus();
  await page.keyboard.type(password);
  await page.keyboard.press("Tab");
  const focusedSubmit = page.getByRole("button", { name: "Sign in" });
  await expect(focusedSubmit).toBeFocused();
  const focusStyle = await focusedSubmit.evaluate((element) => {
    const style = getComputedStyle(element);
    return {
      outlineStyle: style.outlineStyle,
      outlineWidth: style.outlineWidth,
      boxShadow: style.boxShadow,
    };
  });
  expect(
    (focusStyle.outlineStyle !== "none" && parseFloat(focusStyle.outlineWidth) > 0)
      || focusStyle.boxShadow !== "none",
  ).toBe(true);

  const loginResponsePromise = page.waitForResponse((response) =>
    response.url().endsWith("/api/v1/auth/login") && response.request().method() === "POST");
  await page.getByLabel("password").fill(password);
  await page.getByRole("button", { name: "Sign in" }).click();
  const loginResponse = await loginResponsePromise;
  expect(loginResponse.status()).toBe(200);
  const csrf = (await loginResponse.json() as { csrf_token: string }).csrf_token;
  await expect(page.getByRole("heading", { name: "Project Command Center" })).toBeVisible();
  await expect(page.getByText("Ollama reachable")).toBeVisible();
  await runAxe(page);

  await page.getByRole("link", { name: "Chat", exact: true }).click();
  await page.getByLabel("Model").selectOption("fake-fast");
  await page.getByRole("textbox", { name: "Message" }).fill("first real-stack turn");
  await page.getByRole("button", { name: "Send" }).click();
  await expect(page.getByText("Integration response: first real-stack turn")).toBeVisible();
  await expect(page.getByText("Private deterministic reasoning.")).toBeHidden();
  await page.getByText("Model reasoning").click();
  await expect(page.getByText("Private deterministic reasoning.")).toBeVisible();

  const conversationsResponse = await page.request.get("/api/v1/chat/sessions?limit=100");
  expect(conversationsResponse.status()).toBe(200);
  const conversations = (await conversationsResponse.json() as {
    conversations: Array<{ id: string; title: string; model: string; state: string }>;
  }).conversations;
  const first = conversations.find((item) => item.title === "first real-stack turn");
  expect(first?.model).toBe("fake-fast");
  expect(first).toBeDefined();
  const firstSaved = await page.request.get(`/api/v1/chat/sessions/${first!.id}`);
  const firstContent = await firstSaved.json() as {
    messages: Array<{ role: string; content: string; status: string }>;
  };
  expect(firstContent.messages.some((message) =>
    message.role === "assistant" && message.content === "Integration response: first real-stack turn"
      && message.status === "complete")).toBe(true);

  await page.getByRole("button", { name: "New chat" }).click();
  await expect(page).toHaveURL(/\/chat$/);
  await expect(page.getByRole("heading", { name: "What would you like to talk about?" })).toBeVisible();
  await page.getByLabel("Model").selectOption("fake-second");
  await expect(page.getByLabel("Model")).toHaveValue("fake-second");
  await page.getByRole("textbox", { name: "Message" }).fill("second model turn");
  await expect(page.getByLabel("Model")).toHaveValue("fake-second");
  const secondTurnRequest = page.waitForRequest((request) =>
    request.url().includes("/api/v1/chat/sessions/") && request.url().endsWith("/turns"));
  await page.getByRole("button", { name: "Send" }).click();
  const secondTurnPayload = JSON.parse((await secondTurnRequest).postData() ?? "{}") as {
    model?: string;
  };
  expect(secondTurnPayload.model).toBe("fake-second");
  await expect(page.getByText("Integration response: second model turn")).toBeVisible();
  const latestResponse = await page.request.get("/api/v1/chat/sessions?limit=100");
  const latest = (await latestResponse.json() as {
    conversations: Array<{ id: string; title: string; model: string; state: string }>;
  }).conversations;
  const second = latest.find((item) => item.title === "second model turn");
  expect(second?.model).toBe("fake-second");
  await expect.poll(async () => {
    const response = await page.request.get("/api/v1/chat/sessions?limit=100");
    const values = (await response.json() as { conversations: typeof latest }).conversations;
    return values.find((item) => item.id === second?.id)?.state;
  }).toBe("idle");
  await page.goto(`/chat/${first!.id}`);
  await expect(page.getByRole("combobox", { name: "Model" })).toHaveValue("fake-fast");
  await expect(page.getByText("Integration response: first real-stack turn")).toBeVisible();
  await page.goto(`/chat/${second!.id}`);
  await expect(page.getByRole("combobox", { name: "Model" })).toHaveValue("fake-second");
  await runAxe(page);

  await page.getByRole("button", { name: "New chat" }).click();
  await expect(page).toHaveURL(/\/chat$/);
  await expect(page.getByRole("heading", { name: "What would you like to talk about?" })).toBeVisible();
  await page.getByLabel("Model").selectOption("fake-slow");
  await expect(page.getByLabel("Model")).toHaveValue("fake-slow");
  await page.getByRole("textbox", { name: "Message" }).fill("hold until cancellation");
  await expect(page.getByLabel("Model")).toHaveValue("fake-slow");
  const sendButton = page.getByRole("button", { name: "Send" });
  await expect(sendButton).toBeEnabled();
  const cancellationRequest = page.waitForRequest((request) =>
    request.url().includes("/api/v1/chat/sessions/") && request.url().endsWith("/turns"),
  { timeout: 10_000 }).catch(() => null);
  await sendButton.click();
  const cancellationTurn = await cancellationRequest;
  if (!cancellationTurn) {
    throw new Error(`Cancellation turn was not submitted: ${
      await page.locator(".chat-live-status").textContent()
    }`);
  }
  const cancellationPayload = JSON.parse(cancellationTurn.postData() ?? "{}") as {
    model?: string;
  };
  expect(cancellationPayload.model).toBe("fake-slow");
  await expect(page.getByText("Partial output before cancellation.")).toBeVisible();
  await page.getByRole("button", { name: "Stop generation" }).click();
  await expect(page.getByRole("status")).toContainText("Generation stopped.");
  const cancelledList = await page.request.get("/api/v1/chat/sessions?limit=100");
  const cancelledConversations = (await cancelledList.json() as {
    conversations: Array<{ id: string; title: string; state: string }>;
  }).conversations;
  const cancelled = cancelledConversations.find((item) => item.title === "hold until cancellation");
  expect(cancelled?.state).toBe("cancelled");
  const cancelledHistory = await page.request.get(`/api/v1/chat/sessions/${cancelled!.id}`);
  const cancelledData = await cancelledHistory.json() as {
    messages: Array<{ role: string; content: string; status: string }>;
  };
  expect(cancelledData.messages.some((message) =>
    message.role === "assistant" && message.content === "Partial output before cancellation."
      && message.status === "cancelled")).toBe(true);

  await page.getByRole("button", { name: "New chat" }).click();
  await expect(page).toHaveURL(/\/chat$/);
  await expect(page.getByRole("heading", { name: "What would you like to talk about?" })).toBeVisible();
  await page.getByLabel("Model").selectOption("fake-tool");
  await expect(page.getByLabel("Model")).toHaveValue("fake-tool");
  await page.getByRole("textbox", { name: "Message" }).fill("return an unsupported function call");
  const unsupportedToolRequest = page.waitForRequest((request) =>
    request.url().includes("/api/v1/chat/sessions/") && request.url().endsWith("/turns"),
  );
  await page.getByRole("button", { name: "Send" }).click();
  const toolTurn = JSON.parse((await unsupportedToolRequest).postData() ?? "{}") as {
    model?: string;
  };
  expect(toolTurn.model).toBe("fake-tool");
  await expect.poll(async () => {
    const response = await page.request.get("/api/v1/chat/sessions?limit=100");
    const values = (await response.json() as {
      conversations: Array<{ title: string; state: string }>;
    }).conversations;
    return values.find((item) => item.title === "return an unsupported function call")?.state;
  }).toBe("error");
  expect(existsSync(process.env.SYNAI_E2E_TOOL_SENTINEL ?? "")).toBe(false);
  const toolCalls = readFileSync(providerCalls!, "utf-8").trim().split("\n").filter(Boolean)
    .map((line) => JSON.parse(line) as { model: string; tools: number })
    .filter((call) => call.model === "fake-tool");
  expect(toolCalls.at(-1)?.tools).toBe(0);

  await page.getByRole("button", { name: "New chat" }).click();
  await expect(page).toHaveURL(/\/chat$/);
  await expect(page.getByRole("heading", { name: "What would you like to talk about?" })).toBeVisible();
  await page.getByLabel("Model").selectOption("fake-fast");
  await expect(page.getByLabel("Model")).toHaveValue("fake-fast");
  await page.evaluate(() => {
    (window as Window & { __synaiInjected?: boolean }).__synaiInjected = false;
  });
  await page.getByRole("textbox", { name: "Message" }).fill("html injection");
  await page.getByRole("button", { name: "Send" }).click();
  const unsafeMarkup = '<img src=x onerror="window.__synaiInjected = true">';
  await expect(page.getByText(unsafeMarkup, { exact: true })).toBeVisible();
  await expect(page.locator('img[src="x"]')).toHaveCount(0);
  expect(await page.evaluate(() =>
    (window as Window & { __synaiInjected?: boolean }).__synaiInjected,
  )).toBe(false);

  await page.goto(`/chat/${second!.id}`);
  const callsBeforeReconnect = providerCallCount();
  await expect.poll(() => page.evaluate((id) => {
    const target = window as Window & { __synaiSockets?: WebSocket[] };
    return target.__synaiSockets?.some((socket) =>
      socket.url.includes(id) && socket.readyState === WebSocket.OPEN) ?? false;
  }, second!.id)).toBe(true);

  const socketsBefore = await page.evaluate(() => {
    const target = window as Window & { __synaiSockets?: WebSocket[] };
    return target.__synaiSockets?.length ?? 0;
  });
  await page.evaluate((id) => {
    const target = window as Window & { __synaiSockets?: WebSocket[] };
    const socket = target.__synaiSockets?.findLast((candidate) => candidate.url.includes(id));
    if (!socket || socket.readyState !== WebSocket.OPEN) throw new Error("Expected a live chat WebSocket.");
    socket.close(4000, "acceptance disconnect");
  }, second!.id);
  await expect.poll(() => page.evaluate(() => {
    const target = window as Window & { __synaiSockets?: WebSocket[] };
    return target.__synaiSockets?.length ?? 0;
  })).toBeGreaterThan(socketsBefore);
  expect(providerCallCount()).toBe(callsBeforeReconnect);

  setProviderUnavailable(true);
  await page.goto("/");
  await expect(page.getByText("Ollama unavailable")).toBeVisible();
  await page.getByRole("link", { name: "Chat", exact: true }).click();
  await expect(page.getByText("Model provider unavailable")).toBeVisible();
  await expect(page.getByRole("link", { name: /first real-stack turn/ })).toBeVisible();
  setProviderUnavailable(false);

  const csrfResponse = await page.request.post("/api/v1/auth/csrf", {
    headers: { Origin: appOrigin },
  });
  expect(csrfResponse.status()).toBe(200);
  const currentCsrf = (await csrfResponse.json() as { csrf_token: string }).csrf_token;
  const projectResponse = await page.request.post("/api/v1/logical-projects", {
    headers: { Origin: appOrigin, "X-CSRF-Token": currentCsrf },
    data: { name: projectName, registration_key: "acceptance-registration-key-123456" },
  });
  expect(projectResponse.status()).toBe(201);
  const project = await projectResponse.json() as { id: string; name: string };
  const activityResponse = await page.request.get(`/api/v1/logical-projects/${project.id}/activity`);
  expect(activityResponse.status()).toBe(200);
  const activity = await activityResponse.json() as {
    cursor: number; events: Array<{ event_id: number; type: string }>;
  };
  expect(activity.cursor).toBeGreaterThan(0);
  expect(activity.events.map((event) => event.event_id)).toEqual(
    [...activity.events].map((event) => event.event_id).sort((left, right) => left - right),
  );
  expect(activity.events[0].type).toBe("project_created");

  await page.goto(`/workbench/${project.id}`);
  await expect(page.getByRole("heading", { level: 1, name: project.name })).toBeVisible();
  await page.getByRole("button", { name: "Activity" }).click();
  await expect(page.getByText("Project created")).toBeVisible();
  await expect(page.getByText("Live stream connected.")).toBeVisible();
  await runAxe(page);

  const projectSocketsBeforeRestart = await page.evaluate((id) => {
    const target = window as Window & { __synaiSockets?: WebSocket[] };
    return target.__synaiSockets?.filter((socket) => socket.url.includes(`/projects/${id}`)).length ?? 0;
  }, project.id);
  const controlPath = process.env.SYNAI_E2E_CONTROL;
  if (!controlPath) throw new Error("API restart control path is not configured.");
  writeFileSync(controlPath, JSON.stringify({ restart: true }), { mode: 0o600 });
  await expect.poll(() => {
    const state = JSON.parse(readFileSync(controlPath, "utf-8")) as { restart_count?: number };
    return state.restart_count ?? 0;
  }, { timeout: 20_000 }).toBeGreaterThan(0);
  await expect.poll(() => page.evaluate((id) => {
    const target = window as Window & { __synaiSockets?: WebSocket[] };
    return target.__synaiSockets?.filter((socket) => socket.url.includes(`/projects/${id}`)).length ?? 0;
  }, project.id), { timeout: 20_000 }).toBeGreaterThan(projectSocketsBeforeRestart);
  await expect.poll(() => page.evaluate((id) => {
    const target = window as Window & { __synaiSockets?: WebSocket[] };
    return target.__synaiSockets?.findLast((socket) => socket.url.includes(`/projects/${id}`))
      ?.readyState === WebSocket.OPEN;
  }, project.id), { timeout: 20_000 }).toBe(true);
  await expect(page.getByText("Live stream connected.")).toBeVisible();

  const projectStream = await page.evaluate(async (id) => {
    return await new Promise<Array<{ type: string; event_id: number }>>((resolve, reject) => {
      const socket = new WebSocket(
        `ws://${location.host}/api/v1/events/v1/projects/${id}?after=999999`,
      );
      const received: Array<{ type: string; event_id: number }> = [];
      const timeout = setTimeout(() => reject(new Error("Project stream did not resynchronize.")), 5000);
      socket.onmessage = (event) => {
        const value = JSON.parse(String(event.data)) as { type: string; event_id: number };
        received.push(value);
        if (received.length === 2) {
          clearTimeout(timeout);
          socket.close();
          resolve(received);
        }
      };
      socket.onerror = () => reject(new Error("Project stream connection failed."));
    });
  }, project.id);
  expect(projectStream.map((event) => event.type)).toEqual([
    "resynchronization_required", "project_snapshot",
  ]);
  expect(projectStream[1].event_id).toBe(activity.cursor);

  const missingProjectClose = await page.evaluate(async () => await new Promise<number>((resolve, reject) => {
    const socket = new WebSocket(
      `ws://${location.host}/api/v1/events/v1/projects/${"f".repeat(32)}`,
    );
    const timeout = setTimeout(() => reject(new Error("Unauthorized project stream did not close.")), 5000);
    socket.onclose = (event) => { clearTimeout(timeout); resolve(event.code); };
  }));
  expect(missingProjectClose).toBe(1006);

  const beforeLogoutCalls = providerCallCount();
  const logoutCsrfResponse = await page.request.post("/api/v1/auth/csrf", {
    headers: { Origin: appOrigin },
  });
  expect(logoutCsrfResponse.status()).toBe(200);
  const logoutCsrf = (await logoutCsrfResponse.json() as { csrf_token: string }).csrf_token;
  const logout = await page.request.post("/api/v1/auth/logout", {
    headers: { Origin: appOrigin, "X-CSRF-Token": logoutCsrf },
  });
  expect(logout.status()).toBe(200);
  await expect.poll(() => page.evaluate((id) => {
    const target = window as Window & { __synaiSockets?: WebSocket[] };
    return target.__synaiSockets?.some((socket) =>
      socket.url.includes(`/projects/${id}`) && socket.readyState === WebSocket.CLOSED);
  }, project.id)).toBe(true);
  await page.goto("/devices");
  await expect(page.getByRole("heading", { name: "Sign in" })).toBeVisible();
  expect(providerCallCount()).toBe(beforeLogoutCalls);
});

test("responsive navigation, inspector keyboard resize, reduced motion, and code overflow", async ({ page }) => {
  test.setTimeout(90_000);
  await page.goto("/");
  await page.addInitScript(() => {
    const NativeWebSocket = window.WebSocket;
    const sockets: WebSocket[] = [];
    class TrackedWebSocket extends NativeWebSocket {
      constructor(url: string | URL, protocols?: string | string[]) {
        super(url, protocols);
        sockets.push(this);
      }
    }
    Object.defineProperty(window, "WebSocket", { value: TrackedWebSocket });
    Object.defineProperty(window, "__synaiSockets", { value: sockets });
  });
  const loginResponsePromise = page.waitForResponse((response) =>
    response.url().endsWith("/api/v1/auth/login") && response.request().method() === "POST");
  await page.getByLabel("password").fill(password);
  await page.getByRole("button", { name: "Sign in" }).click();
  const loginResponse = await loginResponsePromise;
  const csrf = (await loginResponse.json() as { csrf_token: string }).csrf_token;

  const projectResponse = await page.request.post("/api/v1/logical-projects", {
    headers: { Origin: appOrigin, "X-CSRF-Token": csrf },
    data: { name: `${projectName} layout`, registration_key: "layout-registration-key-12345678" },
  });
  const project = await projectResponse.json() as { id: string; name: string };
  await page.setViewportSize({ width: 820, height: 1024 });
  await page.goto(`/workbench/${project.id}`);
  await expect(page.getByRole("heading", { level: 1, name: project.name })).toBeVisible();
  const inspector = page.getByRole("slider", { name: "Inspector width" });
  const originalWidth = Number(await inspector.inputValue());
  await inspector.focus();
  await page.keyboard.press("ArrowRight");
  expect(Number(await inspector.inputValue())).toBeGreaterThan(originalWidth);
  await runAxe(page);

  await page.emulateMedia({ reducedMotion: "reduce" });
  const transition = await page.getByRole("button", { name: "Hide details" }).evaluate(
    (element) => getComputedStyle(element).transitionDuration,
  );
  expect(transition.split(",").every((value) => Number.parseFloat(value) < 0.01)).toBe(true);

  await page.setViewportSize({ width: 390, height: 844 });
  const menu = page.getByRole("button", { name: "Open navigation" });
  await menu.click();
  const dialog = page.getByRole("dialog", { name: "Navigation" });
  await expect(dialog).toBeVisible();
  await page.keyboard.press("Tab");
  await expect(dialog.locator(":focus")).toHaveCount(1);
  await page.keyboard.press("Escape");
  await expect(dialog).toHaveCount(0);
  await expect(menu).toBeFocused();
  await runAxe(page);

  await page.evaluate(() => { document.documentElement.style.fontSize = "200%"; });
  const dimensions = await page.evaluate(() => ({
    width: document.documentElement.scrollWidth,
    viewport: window.innerWidth,
  }));
  expect(dimensions.width).toBeLessThanOrEqual(dimensions.viewport);

  await page.getByRole("link", { name: "Chat", exact: true }).click();
  await page.getByLabel("Model").selectOption("fake-fast");
  await page.getByRole("textbox", { name: "Message" }).fill("long code");
  await page.getByRole("button", { name: "Send" }).click();
  await expect.poll(async () => {
    const response = await page.request.get("/api/v1/chat/sessions?limit=100");
    const values = (await response.json() as {
      conversations: Array<{ title: string; state: string }>;
    }).conversations;
    return values.find((item) => item.title === "long code")?.state;
  }).toBe("idle");
  const codeConversationsResponse = await page.request.get("/api/v1/chat/sessions?limit=100");
  const codeConversations = (await codeConversationsResponse.json() as {
    conversations: Array<{ id: string; title: string }>;
  }).conversations;
  const codeConversation = codeConversations.find((item) => item.title === "long code");
  expect(codeConversation).toBeDefined();
  const codeHistory = await page.request.get(`/api/v1/chat/sessions/${codeConversation!.id}`);
  const codeData = await codeHistory.json() as {
    messages: Array<{ role: string; content: string }>;
  };
  expect(codeData.messages.some((message) =>
    message.role === "assistant" && message.content.startsWith("```text\n")
      && message.content.includes("x".repeat(100)))).toBe(true);
  await page.goto(`/chat/${codeConversation!.id}`);
  const code = page.locator(".code-block pre");
  await expect(code).toBeVisible();
  const overflow = await code.evaluate((element) => ({
    client: element.clientWidth,
    scroll: element.scrollWidth,
  }));
  expect(overflow.scroll).toBeGreaterThan(overflow.client);
  await runAxe(page);
});

test("a real WebSocket gap refreshes authoritative chat state without resubmitting", async ({ page }) => {
  test.setTimeout(90_000);
  let droppedDelta = false;
  const sockets: WebSocketRoute[] = [];
  await page.routeWebSocket("**/api/v1/events/v1/chat/**", async (socket) => {
    const server = await socket.connectToServer();
    sockets.push(socket);
    socket.onMessage((message) => server.send(message));
    server.onMessage((message) => {
      if (typeof message === "string") {
        try {
          const event = JSON.parse(message) as { type?: string };
          if (event.type === "content_delta" && !droppedDelta) {
            droppedDelta = true;
            return;
          }
        } catch {
          // Non-JSON frames are forwarded for the server's normal validation path.
        }
      }
      socket.send(message);
    });
  });
  await page.goto("/");
  const loginResponsePromise = page.waitForResponse((response) =>
    response.url().endsWith("/api/v1/auth/login") && response.request().method() === "POST");
  await page.getByLabel("password").fill(password);
  await page.getByRole("button", { name: "Sign in" }).click();
  const loginResponse = await loginResponsePromise;
  const csrf = (await loginResponse.json() as { csrf_token: string }).csrf_token;
  const before = providerCallCount();

  await page.getByRole("link", { name: "Chat", exact: true }).click();
  await page.getByLabel("Model").selectOption("fake-fast");
  await page.getByRole("textbox", { name: "Message" }).fill("gap recovery test");
  await page.getByRole("button", { name: "Send" }).click();
  await expect(page.getByText(/live events were missed/i)).toBeVisible();
  expect(droppedDelta).toBe(true);
  const list = await page.request.get("/api/v1/chat/sessions?limit=100");
  const items = (await list.json() as { conversations: Array<{ id: string; title: string }> }).conversations;
  const saved = items.find((item) => item.title === "gap recovery test");
  expect(saved).toBeDefined();
  const transcript = await page.request.get(`/api/v1/chat/sessions/${saved!.id}`);
  const data = await transcript.json() as { messages: Array<{ content: string; role: string }> };
  expect(data.messages.some((message) =>
    message.role === "assistant" && message.content === "Integration response: gap recovery test")).toBe(true);
  expect(providerCallCount()).toBe(before + 1);
  expect(sockets.length).toBeGreaterThan(0);
  expect(csrf).toBeTruthy();
});
