import { defineConfig, devices } from "@playwright/test";
import { API_ORIGIN, API_PORT, WEB_PORT } from "./e2e/support";

// Browser tests through the real same-origin path: Chromium -> `next start` (a production
// build) -> the /api/* rewrite -> FastAPI. The backend is tests/web_e2e/server.py: the real
// app with scripted models and recorded search, so no keys and no paid calls.
// Tests share one backend (its fetch gate and clock), so they run one at a time.

export default defineConfig({
  testDir: "./e2e",
  fullyParallel: false,
  workers: 1,
  retries: 0,
  timeout: 90_000,
  expect: { timeout: 30_000 },
  reporter: [["list"]],
  use: {
    baseURL: `http://localhost:${WEB_PORT}`,
    trace: "retain-on-failure",
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
  webServer: [
    {
      command: "uv run python -m tests.web_e2e.server",
      cwd: "..",
      url: `${API_ORIGIN}/personas`,
      env: { E2E_PORT: String(API_PORT), E2E_SSE_SECONDS: "3" },
      timeout: 120_000,
      reuseExistingServer: false,
    },
    {
      command: `npm run build && npm run start -- -p ${WEB_PORT}`,
      url: `http://localhost:${WEB_PORT}`,
      env: { INSIGHTFORGE_API_ORIGIN: API_ORIGIN },
      timeout: 300_000,
      reuseExistingServer: false,
    },
  ],
});
