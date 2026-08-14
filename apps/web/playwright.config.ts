import { defineConfig, devices } from "@playwright/test";

export default defineConfig({
  testDir: "./e2e",
  testIgnore: ["**/demo-live.spec.ts", "**/projects-live.spec.ts"],
  fullyParallel: true,
  forbidOnly: Boolean(process.env.CI),
  // Flaky acceptance checks must fail visibly; CI does not conceal them with retries.
  retries: 0,
  workers: process.env.CI ? 2 : 4,
  reporter: process.env.CI ? [["github"], ["html", { open: "never" }]] : "list",
  use: {
    baseURL: "http://localhost:3000",
    screenshot: "only-on-failure",
    trace: "retain-on-failure",
  },
  projects: [
    { name: "chromium", use: { ...devices["Desktop Chrome"] } },
    { name: "firefox", use: { ...devices["Desktop Firefox"] } },
    { name: "webkit", use: { ...devices["Desktop Safari"] } },
  ],
  webServer: {
    command: "node .next/standalone/apps/web/server.js",
    env: {
      ...process.env,
      HOSTNAME: "127.0.0.1",
      PORT: "3000",
      CORPUSKIT_ENVIRONMENT: "test",
      CORPUSKIT_WEB_AUTH_MODE: "test",
      CORPUSKIT_WEB_STATE_SECRET: "QUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUE",
      CORPUSKIT_WEB_SESSION_ENCRYPTION_KEYS:
        '["QkJCQkJCQkJCQkJCQkJCQkJCQkJCQkJCQkJCQkJCQkI"]',
      CORPUSKIT_WEB_ALLOWED_RETURN_PATHS:
        '["/","/projects","/evaluate","/analysis","/capabilities","/g2p","/inventory","/coverage","/selection","/generation","/advanced","/jobs","/artifacts"]',
    },
    url: "http://localhost:3000",
    reuseExistingServer: !process.env.CI,
    timeout: 120_000,
  },
});
