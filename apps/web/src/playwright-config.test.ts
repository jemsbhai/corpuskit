import { afterEach, describe, expect, it, vi } from "vitest";

import standardConfig from "../playwright.config";

function patterns(value: unknown): readonly unknown[] {
  return Array.isArray(value) ? value : [value];
}

describe("Playwright suite boundaries", () => {
  afterEach(() => vi.unstubAllEnvs());

  it("keeps the real-stack demo out of the standard mocked browser matrix", () => {
    expect(patterns(standardConfig.testIgnore)).toContain(
      "**/demo-live.spec.ts",
    );
  });

  it("selects only the real-stack demo in the explicit live configuration", async () => {
    vi.stubEnv("CORPUSKIT_LIVE_BASE_URL", "http://127.0.0.1:3000");
    const { default: liveConfig } = await import("../playwright.live.config");

    expect(liveConfig.testMatch).toBe("demo-live.spec.ts");
  });
});
