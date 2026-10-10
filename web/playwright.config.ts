import { defineConfig, devices } from "@playwright/test";
import { tmpdir } from "node:os";
import { join } from "node:path";

export default defineConfig({
  testDir: "./e2e",
  testMatch: "**/phase-13d.spec.ts",
  fullyParallel: false,
  reporter: "list",
  outputDir: join(tmpdir(), "synai-phase13d-ui-output"),
  preserveOutput: "never",
  use: {
    baseURL: "http://127.0.0.1:4179",
    ...devices["Desktop Chrome"],
    trace: "off",
    screenshot: "off",
    video: "off",
  },
  webServer: {
    command: "npm run dev -- --host 127.0.0.1 --port 4179 --strictPort",
    url: "http://127.0.0.1:4179",
    reuseExistingServer: !process.env.CI,
    timeout: 30_000,
  },
});
