import { defineConfig, devices } from "@playwright/test";
import { tmpdir } from "node:os";
import { join } from "node:path";

const stateDir = join(tmpdir(), "synai-phase13d-integration");
const origin = "http://127.0.0.1:4179";
const integrationEnv = {
  SYNAI_E2E_PASSWORD: "disposable-browser-acceptance-password",
  SYNAI_E2E_PROVIDER_STATE: join(stateDir, "provider-state.json"),
  SYNAI_E2E_PROVIDER_CALLS: join(stateDir, "provider-calls.jsonl"),
  SYNAI_E2E_TOOL_SENTINEL: join(stateDir, "tool-execution-must-not-occur"),
  SYNAI_E2E_CONTROL: join(stateDir, "control.json"),
};
Object.assign(process.env, integrationEnv);

export default defineConfig({
  testDir: "./e2e/integration",
  fullyParallel: false,
  workers: 1,
  reporter: "list",
  outputDir: join(tmpdir(), "synai-phase13d-playwright-output"),
  preserveOutput: "never",
  use: {
    baseURL: origin,
    ...devices["Desktop Chrome"],
    trace: "off",
    screenshot: "off",
    video: "off",
  },
  webServer: [
    {
      command: "../.venv/bin/python e2e-support/integration_server.py",
      cwd: ".",
      url: "http://127.0.0.1:8765/api/v1/health",
      reuseExistingServer: false,
      timeout: 30_000,
      env: {
        PYTHONPATH: "..",
        ...integrationEnv,
      },
    },
    {
      command: "npm run build && ./node_modules/.bin/vite preview --host 127.0.0.1 --port 4179 --strictPort",
      cwd: ".",
      url: origin,
      reuseExistingServer: false,
      timeout: 30_000,
    },
  ],
});
