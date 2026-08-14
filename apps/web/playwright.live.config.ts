import { defineConfig, devices } from "@playwright/test";

const baseURL = process.env.CORPUSKIT_LIVE_BASE_URL;
if (!baseURL)
  throw new Error(
    "Set CORPUSKIT_LIVE_BASE_URL to the already-running CorpusKit web origin.",
  );

export default defineConfig({
  testDir: "./e2e",
  testMatch: ["demo-live.spec.ts", "projects-live.spec.ts"],
  fullyParallel: false,
  forbidOnly: true,
  retries: 0,
  workers: 1,
  reporter: [
    ["list"],
    ["html", { open: "never", outputFolder: "playwright-live-report" }],
  ],
  use: {
    baseURL,
    screenshot: "only-on-failure",
    trace: "retain-on-failure",
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
});
