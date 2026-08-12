import { describe, expect, it } from "vitest";

import nextConfig from "../../next.config";

describe("production web security headers", () => {
  it("sets global transport protections and a non-HTML deny policy", async () => {
    expect(nextConfig.headers).toBeTypeOf("function");
    const rules = await nextConfig.headers!();
    const globalRule = rules.find(({ source }) => source === "/:path*");
    const headers = new Map(
      globalRule?.headers.map((header) => [header.key, header.value]),
    );

    expect(headers.get("X-Frame-Options")).toBe("DENY");
    expect(headers.get("X-Content-Type-Options")).toBe("nosniff");
    expect(headers.get("Referrer-Policy")).toBe("no-referrer");
    expect(headers.get("Strict-Transport-Security")).toBe(
      "max-age=31536000; includeSubDomains",
    );
    expect(headers.get("Permissions-Policy")).toContain("microphone=()");
    expect(headers.has("Content-Security-Policy")).toBe(false);

    for (const source of ["/api/:path*", "/auth/:path*"]) {
      const rule = rules.find((candidate) => candidate.source === source);
      const policy = rule?.headers.find(
        ({ key }) => key === "Content-Security-Policy",
      )?.value;
      expect(policy).toBe(
        "default-src 'none'; base-uri 'none'; frame-ancestors 'none'",
      );
      expect(policy).not.toContain("script-src");
    }
  });
});
