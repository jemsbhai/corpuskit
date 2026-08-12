import { describe, expect, it } from "vitest";

import { parseWebAuthConfig } from "./config";
import { AuthConfigurationError } from "./types";

function base(overrides: Partial<NodeJS.ProcessEnv> = {}): NodeJS.ProcessEnv {
  return {
    NODE_ENV: "production",
    CORPUSKIT_ENVIRONMENT: "production",
    CORPUSKIT_WEB_AUTH_MODE: "oidc",
    CORPUSKIT_WEB_STATE_SECRET: Buffer.alloc(32, 1).toString("base64url"),
    CORPUSKIT_WEB_SESSION_ENCRYPTION_KEYS: JSON.stringify([
      Buffer.alloc(32, 2).toString("base64url"),
    ]),
    CORPUSKIT_WEB_ALLOWED_RETURN_PATHS: '["/","/projects","/evaluate"]',
    CORPUSKIT_WEB_SESSION_REDIS_URL:
      "rediss://corpuskit:strong-password@cache.example:6379/0",
    CORPUSKIT_OIDC_ISSUER: "https://identity.example/realms/corpuskit",
    CORPUSKIT_OIDC_AUDIENCE: "corpuskit-api",
    CORPUSKIT_WEB_OIDC_CLIENT_ID: "corpuskit-web",
    CORPUSKIT_WEB_OIDC_CLIENT_SECRET: "a-production-secret-value",
    CORPUSKIT_WEB_OIDC_REDIRECT_URI: "https://corpuskit.example/auth/callback",
    ...overrides,
  };
}

describe("web auth configuration", () => {
  it("parses a bounded production OIDC and TLS Redis configuration", () => {
    const config = parseWebAuthConfig(base());
    expect(config.mode).toBe("oidc");
    expect(config.issuer?.href).toBe(
      "https://identity.example/realms/corpuskit",
    );
    expect(config.allowedReturnPaths.has("/projects")).toBe(true);
    expect(config.redisUrl?.protocol).toBe("rediss:");
    expect(config.sessionAbsoluteSeconds).toBe(28_800);
  });

  it("accepts bounded nested and trailing-slash return paths", () => {
    const longest = `/${"a".repeat(127)}`;
    const config = parseWebAuthConfig(
      base({
        CORPUSKIT_WEB_ALLOWED_RETURN_PATHS: JSON.stringify([
          "/",
          "/projects/team-1/~draft_v2./",
          longest,
        ]),
      }),
    );

    expect(config.allowedReturnPaths).toEqual(
      new Set(["/", "/projects/team-1/~draft_v2./", longest]),
    );
  });

  it("rejects a bounded adversarial return path without ambiguous matching", () => {
    const adversarial = `/${"-".repeat(126)}!`;
    expect(adversarial).toHaveLength(128);
    expect(() =>
      parseWebAuthConfig(
        base({
          CORPUSKIT_WEB_ALLOWED_RETURN_PATHS: JSON.stringify([
            "/",
            adversarial,
          ]),
        }),
      ),
    ).toThrow(AuthConfigurationError);
  });

  it.each([
    ["missing mode", { CORPUSKIT_WEB_AUTH_MODE: "" }],
    ["demo production", { CORPUSKIT_WEB_AUTH_MODE: "demo" }],
    ["test production", { CORPUSKIT_WEB_AUTH_MODE: "test" }],
    ["missing state key", { CORPUSKIT_WEB_STATE_SECRET: "" }],
    ["short state key", { CORPUSKIT_WEB_STATE_SECRET: "c2hvcnQ" }],
    [
      "missing encryption key ring",
      { CORPUSKIT_WEB_SESSION_ENCRYPTION_KEYS: "" },
    ],
    [
      "malformed encryption key ring",
      { CORPUSKIT_WEB_SESSION_ENCRYPTION_KEYS: "not-json" },
    ],
    [
      "short encryption key",
      { CORPUSKIT_WEB_SESSION_ENCRYPTION_KEYS: '["c2hvcnQ"]' },
    ],
    [
      "duplicate encryption key",
      {
        CORPUSKIT_WEB_SESSION_ENCRYPTION_KEYS: JSON.stringify([
          Buffer.alloc(32, 2).toString("base64url"),
          Buffer.alloc(32, 2).toString("base64url"),
        ]),
      },
    ],
    [
      "reused state encryption key",
      {
        CORPUSKIT_WEB_SESSION_ENCRYPTION_KEYS: JSON.stringify([
          Buffer.alloc(32, 1).toString("base64url"),
        ]),
      },
    ],
    ["open return", { CORPUSKIT_WEB_ALLOWED_RETURN_PATHS: '["/","//evil"]' }],
    [
      "dot-segment return",
      { CORPUSKIT_WEB_ALLOWED_RETURN_PATHS: '["/","/projects/../analysis"]' },
    ],
    ["return query", { CORPUSKIT_WEB_ALLOWED_RETURN_PATHS: '["/","/x?a=1"]' }],
    ["duplicate return", { CORPUSKIT_WEB_ALLOWED_RETURN_PATHS: '["/","/"]' }],
    [
      "missing root return",
      { CORPUSKIT_WEB_ALLOWED_RETURN_PATHS: '["/projects"]' },
    ],
    [
      "plaintext redis",
      { CORPUSKIT_WEB_SESSION_REDIS_URL: "redis://u:p@cache:6379/0" },
    ],
    [
      "credentialless redis",
      { CORPUSKIT_WEB_SESSION_REDIS_URL: "rediss://cache:6379/0" },
    ],
    [
      "redis query",
      { CORPUSKIT_WEB_SESSION_REDIS_URL: "rediss://u:p@cache:6379/0?x=1" },
    ],
    [
      "redis database",
      { CORPUSKIT_WEB_SESSION_REDIS_URL: "rediss://u:p@cache:6379/99" },
    ],
    [
      "redis missing host",
      { CORPUSKIT_WEB_SESSION_REDIS_URL: "rediss://:password@/0" },
    ],
    ["issuer http", { CORPUSKIT_OIDC_ISSUER: "http://identity.example" }],
    [
      "issuer custom port",
      { CORPUSKIT_OIDC_ISSUER: "https://identity.example:8443" },
    ],
    [
      "issuer well known",
      {
        CORPUSKIT_OIDC_ISSUER:
          "https://identity.example/.well-known/openid-configuration",
      },
    ],
    [
      "redirect query",
      {
        CORPUSKIT_WEB_OIDC_REDIRECT_URI:
          "https://corpuskit.example/auth/callback?x=1",
      },
    ],
    [
      "redirect path",
      { CORPUSKIT_WEB_OIDC_REDIRECT_URI: "https://corpuskit.example/callback" },
    ],
    ["bad audience", { CORPUSKIT_OIDC_AUDIENCE: "contains space" }],
    ["short client secret", { CORPUSKIT_WEB_OIDC_CLIENT_SECRET: "short" }],
    ["missing offline scope", { CORPUSKIT_WEB_OIDC_SCOPES: "openid profile" }],
    [
      "duplicate scope",
      { CORPUSKIT_WEB_OIDC_SCOPES: "openid offline_access openid" },
    ],
    ["bad integer", { CORPUSKIT_WEB_SESSION_IDLE_SECONDS: "NaN" }],
    ["unsafe prefix", { CORPUSKIT_WEB_REDIS_PREFIX: "spaces are unsafe" }],
  ])("rejects %s", (_label, overrides) => {
    expect(() => parseWebAuthConfig(base(overrides))).toThrow(
      AuthConfigurationError,
    );
  });

  it("allows only explicit local modes outside production", () => {
    const shared = {
      NODE_ENV: "test" as const,
      CORPUSKIT_ENVIRONMENT: "development",
      CORPUSKIT_WEB_AUTH_MODE: "demo",
      CORPUSKIT_WEB_STATE_SECRET: Buffer.alloc(32, 2).toString("base64url"),
      CORPUSKIT_WEB_SESSION_ENCRYPTION_KEYS: JSON.stringify([
        Buffer.alloc(32, 3).toString("base64url"),
      ]),
      CORPUSKIT_WEB_ALLOWED_RETURN_PATHS: '["/"]',
    };
    expect(parseWebAuthConfig(shared).mode).toBe("demo");
    expect(() =>
      parseWebAuthConfig({ ...shared, CORPUSKIT_WEB_AUTH_MODE: "test" }),
    ).toThrow(AuthConfigurationError);
    expect(
      parseWebAuthConfig({
        ...shared,
        CORPUSKIT_ENVIRONMENT: "test",
        CORPUSKIT_WEB_AUTH_MODE: "test",
      }).mode,
    ).toBe("test");
  });
});
