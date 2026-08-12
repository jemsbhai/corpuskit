import { NextRequest } from "next/server";
import { describe, expect, it } from "vitest";

import { pageContentSecurityPolicy, proxy } from "./proxy";

describe("request-nonce content security policy", () => {
  it("removes production script unsafe-inline and keeps eval development-only", () => {
    const nonce = "A".repeat(48);
    const production = pageContentSecurityPolicy(nonce, false);
    expect(production).toContain(
      `script-src 'self' 'nonce-${nonce}' 'strict-dynamic'`,
    );
    expect(production).not.toContain("script-src 'self' 'unsafe-inline'");
    expect(production).not.toContain("'unsafe-eval'");
    expect(production).toContain("upgrade-insecure-requests");

    const development = pageContentSecurityPolicy(nonce, true);
    expect(development).toContain("'unsafe-eval'");
    expect(development).not.toContain("script-src 'self' 'unsafe-inline'");
    expect(development).not.toContain("upgrade-insecure-requests");

    const localAcceptance = pageContentSecurityPolicy(nonce, false, false);
    expect(localAcceptance).not.toContain("'unsafe-eval'");
    expect(localAcceptance).not.toContain("script-src 'self' 'unsafe-inline'");
    expect(localAcceptance).not.toContain("upgrade-insecure-requests");
  });

  it("authors a fresh valid response nonce per request", () => {
    const first = proxy(new NextRequest("https://web.example/projects"));
    const second = proxy(new NextRequest("https://web.example/projects"));
    const firstPolicy = first.headers.get("content-security-policy") ?? "";
    const secondPolicy = second.headers.get("content-security-policy") ?? "";
    const firstNonce = /'nonce-([A-Za-z0-9+/]{48})'/u.exec(firstPolicy)?.[1];
    const secondNonce = /'nonce-([A-Za-z0-9+/]{48})'/u.exec(secondPolicy)?.[1];
    expect(firstNonce).toBeTruthy();
    expect(secondNonce).toBeTruthy();
    expect(firstNonce).not.toBe(secondNonce);
  });

  it("rejects a malformed nonce", () => {
    expect(() => pageContentSecurityPolicy("short", false)).toThrow(TypeError);
  });
});
