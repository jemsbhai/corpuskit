import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { GET as callback } from "@/app/auth/callback/route";
import { GET as login } from "@/app/auth/login/route";
import { POST as logout } from "@/app/auth/logout/route";
import { GET as sessionRoute } from "@/app/auth/session/route";
import { setAuthRuntimeForTests } from "@/lib/auth/runtime";
import { AuthService } from "@/lib/auth/service";
import { MemorySessionStore } from "@/lib/auth/session-store";
import type { OidcProvider, WebAuthConfig } from "@/lib/auth/types";

const config: WebAuthConfig = {
  mode: "oidc",
  environment: "test",
  issuer: new URL("https://identity.example"),
  audience: "corpuskit-api",
  clientId: "corpuskit-web",
  clientSecret: "secret-value-long-enough",
  redirectUri: new URL("https://web.example/auth/callback"),
  stateSecret: new Uint8Array(Buffer.alloc(32, 41)),
  sessionEncryptionKeys: [new Uint8Array(Buffer.alloc(32, 42))],
  scopes: "openid profile offline_access",
  allowedReturnPaths: new Set(["/", "/projects"]),
  sessionAbsoluteSeconds: 3_600,
  sessionIdleSeconds: 600,
  refreshBeforeSeconds: 60,
  transactionSeconds: 600,
  oidcTimeoutSeconds: 5,
  redisCommandTimeoutMs: 1_000,
  redisPrefix: "test:web",
};

let oidc: OidcProvider & Record<"revoke", ReturnType<typeof vi.fn>>;

beforeEach(() => {
  oidc = {
    authorizationUrl: vi.fn(async ({ state }) => {
      const url = new URL("https://identity.example/authorize");
      url.searchParams.set("state", state);
      return url;
    }),
    exchange: vi.fn(async () => ({
      subject: "user-1",
      displayName: "User One",
      accessToken: "access-secret",
      refreshToken: "refresh-secret",
      idToken: "identity-secret",
      expiresAt: Date.now() + 300_000,
    })),
    refresh: vi.fn(),
    revoke: vi.fn(async () => undefined),
  };
  setAuthRuntimeForTests({
    service: new AuthService(config, new MemorySessionStore(), oidc),
  });
});

afterEach(() => {
  setAuthRuntimeForTests(undefined);
});

function cookieValue(header: string, name: string): string {
  const match = new RegExp(`${name}=([A-Za-z0-9_-]{43,128})`, "u").exec(header);
  if (!match?.[1]) throw new Error(`Missing ${name} cookie.`);
  return match[1];
}

describe("OIDC route flow", () => {
  it("sets strict correlation/session cookies and never returns token material", async () => {
    const loginResponse = await login(
      new Request("https://web.example/auth/login?returnTo=%2Fprojects"),
    );
    expect(loginResponse.status).toBe(302);
    expect(loginResponse.headers.get("location")).toMatch(
      /^https:\/\/identity\.example\/authorize\?/u,
    );
    const loginCookie = loginResponse.headers.get("set-cookie") ?? "";
    expect(loginCookie).toContain("__Host-corpuskit_login=");
    expect(loginCookie).toContain("Secure");
    expect(loginCookie).toContain("HttpOnly");
    expect(loginCookie).toContain("SameSite=Lax");
    expect(loginCookie).not.toContain("Domain=");
    const correlation = cookieValue(loginCookie, "__Host-corpuskit_login");
    const state = new URL(
      loginResponse.headers.get("location") ?? "",
    ).searchParams.get("state");

    const callbackResponse = await callback(
      new Request(
        `https://web.example/auth/callback?code=one&state=${encodeURIComponent(state ?? "")}`,
        { headers: { cookie: `__Host-corpuskit_login=${correlation}` } },
      ),
    );
    expect(callbackResponse.status).toBe(303);
    expect(callbackResponse.headers.get("location")).toBe(
      "https://web.example/projects",
    );
    const callbackCookies = callbackResponse.headers.get("set-cookie") ?? "";
    expect(callbackCookies).toContain("__Host-corpuskit_login=;");
    expect(callbackCookies).toContain("__Host-corpuskit_session=");
    const serialized = [...callbackResponse.headers.entries()].join("\n");
    for (const secret of [
      "access-secret",
      "refresh-secret",
      "identity-secret",
    ]) {
      expect(serialized).not.toContain(secret);
    }

    const sessionId = cookieValue(callbackCookies, "__Host-corpuskit_session");
    const sessionResponse = await sessionRoute(
      new Request("https://web.example/auth/session", {
        headers: { cookie: `__Host-corpuskit_session=${sessionId}` },
      }),
    );
    const body = (await sessionResponse.json()) as { csrfToken: string };
    expect(sessionResponse.status).toBe(200);
    expect(body.csrfToken).toMatch(/^[A-Za-z0-9_-]{43}$/u);
    expect(JSON.stringify(body)).not.toContain("access-secret");

    const deniedLogout = await logout(
      new Request("https://web.example/auth/logout", {
        method: "POST",
        headers: {
          cookie: `__Host-corpuskit_session=${sessionId}`,
          "x-corpuskit-csrf": "wrong",
        },
      }),
    );
    expect(deniedLogout.status).toBe(403);

    const logoutResponse = await logout(
      new Request("https://web.example/auth/logout", {
        method: "POST",
        headers: {
          cookie: `__Host-corpuskit_session=${sessionId}`,
          "x-corpuskit-csrf": body.csrfToken,
        },
      }),
    );
    expect(logoutResponse.status).toBe(204);
    expect(logoutResponse.headers.get("set-cookie")).toContain("Max-Age=0");
    expect(oidc.revoke).toHaveBeenCalledTimes(2);
    const missing = await sessionRoute(
      new Request("https://web.example/auth/session", {
        headers: { cookie: `__Host-corpuskit_session=${sessionId}` },
      }),
    );
    await expect(missing.json()).resolves.toEqual({ authenticated: false });
  });

  it("rejects an oversized callback before provider exchange and clears correlation", async () => {
    const started = await login(new Request("https://web.example/auth/login"));
    const loginCookie = started.headers.get("set-cookie") ?? "";
    const correlation = cookieValue(loginCookie, "__Host-corpuskit_login");
    const state = new URL(
      started.headers.get("location") ?? "",
    ).searchParams.get("state");
    const prefix = "https://web.example/auth/callback?code=";
    const suffix = `&state=${encodeURIComponent(state ?? "")}`;
    const request = new Request(
      `${prefix}${"a".repeat(8_193 - prefix.length - suffix.length)}${suffix}`,
      { headers: { cookie: `__Host-corpuskit_login=${correlation}` } },
    );
    expect(request.url).toHaveLength(8_193);

    const response = await callback(request);

    expect(response.status).toBe(400);
    expect(await response.json()).toMatchObject({
      code: "invalid_authentication_callback",
    });
    expect(response.headers.get("set-cookie")).toContain(
      "__Host-corpuskit_login=;",
    );
    expect(response.headers.get("cache-control")).toBe("no-store");
    expect(oidc.exchange).not.toHaveBeenCalled();
  });

  it("allows the exact callback URL limit to reach the authenticated exchange", async () => {
    const started = await login(new Request("https://web.example/auth/login"));
    const loginCookie = started.headers.get("set-cookie") ?? "";
    const correlation = cookieValue(loginCookie, "__Host-corpuskit_login");
    const state = new URL(
      started.headers.get("location") ?? "",
    ).searchParams.get("state");
    const prefix = "https://web.example/auth/callback?code=";
    const suffix = `&state=${encodeURIComponent(state ?? "")}`;
    const callbackUrl = `${prefix}${"a".repeat(
      8_192 - prefix.length - suffix.length,
    )}${suffix}`;
    expect(callbackUrl).toHaveLength(8_192);

    const response = await callback(
      new Request(callbackUrl, {
        headers: { cookie: `__Host-corpuskit_login=${correlation}` },
      }),
    );

    expect(response.status).toBe(303);
    expect(oidc.exchange).toHaveBeenCalledTimes(1);
  });

  it("rejects open redirects, duplicate return parameters, and correlation mismatch", async () => {
    for (const url of [
      "https://web.example/auth/login?returnTo=https%3A%2F%2Fevil.example",
      "https://web.example/auth/login?returnTo=%2F&returnTo=%2Fprojects",
    ]) {
      const response = await login(new Request(url));
      expect(response.status).toBe(400);
      expect(response.headers.get("location")).toBeNull();
    }

    const started = await login(new Request("https://web.example/auth/login"));
    const state = new URL(
      started.headers.get("location") ?? "",
    ).searchParams.get("state");
    const response = await callback(
      new Request(
        `https://web.example/auth/callback?code=one&state=${encodeURIComponent(state ?? "")}`,
        {
          headers: {
            cookie: `__Host-corpuskit_login=${Buffer.alloc(32, 99).toString("base64url")}`,
          },
        },
      ),
    );
    expect(response.status).toBe(400);
    expect(response.headers.get("set-cookie")).toContain(
      "__Host-corpuskit_login=;",
    );
  });
});
