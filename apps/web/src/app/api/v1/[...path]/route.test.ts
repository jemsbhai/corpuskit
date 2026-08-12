import { createHash } from "node:crypto";

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { setAuthRuntimeForTests } from "@/lib/auth/runtime";
import { AuthService } from "@/lib/auth/service";
import { MemorySessionStore } from "@/lib/auth/session-store";
import type { BrowserSession, WebAuthConfig } from "@/lib/auth/types";

import { GET, POST } from "./route";

const context = (path: string[]) => ({ params: Promise.resolve({ path }) });

const authConfig = (mode: "oidc" | "test" = "test"): WebAuthConfig => ({
  mode,
  environment: "test",
  ...(mode === "oidc"
    ? {
        issuer: new URL("https://identity.example"),
        audience: "corpuskit-api",
        clientId: "corpuskit-web",
        clientSecret: "secret-value-long-enough",
        redirectUri: new URL("https://web.example/auth/callback"),
      }
    : {}),
  stateSecret: new Uint8Array(Buffer.alloc(32, 1)),
  sessionEncryptionKeys: [new Uint8Array(Buffer.alloc(32, 2))],
  scopes: mode === "oidc" ? "openid offline_access" : "",
  allowedReturnPaths: new Set(["/"]),
  sessionAbsoluteSeconds: 3_600,
  sessionIdleSeconds: 600,
  refreshBeforeSeconds: 60,
  transactionSeconds: 600,
  oidcTimeoutSeconds: 5,
  redisCommandTimeoutMs: 1_000,
  redisPrefix: "test:web",
});

let authenticatedHeaders: Record<string, string>;

beforeEach(async () => {
  const service = new AuthService(authConfig(), new MemorySessionStore());
  const session = await service.bootstrapLocalSession();
  authenticatedHeaders = {
    cookie: `__Host-corpuskit_session=${session.id}`,
    "x-corpuskit-csrf": session.value.csrfToken,
  };
  setAuthRuntimeForTests({ service });
});

afterEach(() => {
  setAuthRuntimeForTests(undefined);
  vi.unstubAllGlobals();
  vi.unstubAllEnvs();
});

describe("API BFF proxy", () => {
  it("forwards only allowlisted request and response metadata", async () => {
    vi.stubEnv("CORPUSKIT_API_INTERNAL_URL", "http://api:8000/api/v1/");
    const upstreamBody = '{"ok":true}';
    const upstreamSha256 = createHash("sha256").update(upstreamBody).digest();
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(upstreamBody, {
        status: 201,
        headers: {
          "content-disposition":
            "attachment; filename=\"seed-v1.txt\"; filename*=UTF-8''seed-v1.txt",
          "content-type": "application/json",
          "content-digest": `sha-256=:${upstreamSha256.toString("base64")}:`,
          etag: `"${upstreamSha256.toString("hex")}"`,
          "set-cookie": "must-not-leak=1",
          "x-content-sha256": upstreamSha256.toString("hex"),
          "x-request-id": "request-1",
        },
      }),
    );
    vi.stubGlobal("fetch", fetchMock);

    const response = await POST(
      new Request("http://web/api/v1/analyses/distribution?mode=full", {
        method: "POST",
        headers: {
          authorization: "Bearer must-not-forward",
          "content-type": "application/json",
          "idempotency-key": "selection-42",
          "x-request-id": "request-1",
          ...authenticatedHeaders,
        },
        body: '{"counts":[]}',
      }),
      context(["analyses", "distribution"]),
    );

    const [url, init] = fetchMock.mock.calls[0] as [URL, RequestInit];
    expect(url.toString()).toBe(
      "http://api:8000/api/v1/analyses/distribution?mode=full",
    );
    expect(new Headers(init.headers).get("authorization")).toBeNull();
    expect(new Headers(init.headers).get("idempotency-key")).toBe(
      "selection-42",
    );
    expect(new Headers(init.headers).get("x-request-id")).toBe("request-1");
    expect(new TextDecoder().decode(init.body as ArrayBuffer)).toBe(
      '{"counts":[]}',
    );
    expect(response.status).toBe(201);
    expect(response.headers.get("cache-control")).toBe("no-store");
    expect(response.headers.get("x-request-id")).toBe("request-1");
    expect(response.headers.get("content-disposition")).toContain(
      "filename*=UTF-8''seed-v1.txt",
    );
    expect(response.headers.get("content-digest")).toBe(
      `sha-256=:${upstreamSha256.toString("base64")}:`,
    );
    expect(response.headers.get("etag")).toBe(
      `"${upstreamSha256.toString("hex")}"`,
    );
    expect(response.headers.get("x-content-sha256")).toBe(
      upstreamSha256.toString("hex"),
    );
    expect(response.headers.get("set-cookie")).toContain(
      "__Host-corpuskit_session=",
    );
    expect(response.headers.get("set-cookie")).not.toContain("must-not-leak");
    const forwardedBytes = Buffer.from(await response.arrayBuffer());
    expect(createHash("sha256").update(forwardedBytes).digest("hex")).toBe(
      upstreamSha256.toString("hex"),
    );
  });

  it("drops malformed download metadata from the upstream", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response("data", {
          headers: {
            "content-disposition": "inline; filename=secret.txt",
            "content-digest": "md5=:unsafe:",
            etag: "weak-etag",
            "x-content-sha256": "not-a-digest",
          },
        }),
      ),
    );
    const response = await GET(
      new Request("http://web/api/v1/projects/export"),
      context(["projects", "export"]),
    );
    expect(response.headers.get("content-disposition")).toBeNull();
    expect(response.headers.get("content-digest")).toBeNull();
    expect(response.headers.get("etag")).toBeNull();
    expect(response.headers.get("x-content-sha256")).toBeNull();
  });

  it("uses a safe local default and reports an unreachable API without details", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockRejectedValue(new Error("private network detail")),
    );
    const response = await GET(
      new Request("http://web/api/v1/capabilities"),
      context(["capabilities"]),
    );
    expect(response.status).toBe(503);
    expect(response.headers.get("cache-control")).toBe("no-store");
    expect(await response.json()).toMatchObject({
      code: "api_upstream_unavailable",
    });
  });

  it("rejects an invalid internal URL", async () => {
    vi.stubEnv("CORPUSKIT_API_INTERNAL_URL", "file:///private/data");
    const response = await GET(
      new Request("http://web/api/v1/capabilities", {
        headers: { "x-request-id": "safe-id" },
      }),
      context(["capabilities"]),
    );
    expect(response.status).toBe(503);
    expect(await response.json()).toMatchObject({
      code: "api_proxy_unavailable",
      request_id: "safe-id",
    });
  });

  it.each([
    "http://user:secret@api:8000/api/v1/",
    "http://api:8000/api/v1/?tenant=other",
    "http://api:8000/other/",
  ])("rejects a non-fixed internal API base: %s", async (value) => {
    vi.stubEnv("CORPUSKIT_API_INTERNAL_URL", value);
    const response = await GET(
      new Request("http://web/api/v1/capabilities"),
      context(["capabilities"]),
    );
    expect(response.status).toBe(503);
    expect(await response.json()).toMatchObject({
      code: "api_proxy_unavailable",
    });
  });

  it("fails closed without an explicit API base in production", async () => {
    vi.stubEnv("CORPUSKIT_ENVIRONMENT", "production");
    vi.stubEnv("CORPUSKIT_API_INTERNAL_URL", "");
    const response = await GET(
      new Request("https://web.example/api/v1/capabilities"),
      context(["capabilities"]),
    );
    expect(response.status).toBe(503);
    expect(await response.json()).toMatchObject({
      code: "api_proxy_unavailable",
    });
  });

  it("does not reflect hostile identifiers and rejects unsafe paths", async () => {
    const response = await GET(
      new Request("http://web/api/v1/empty", {
        headers: { "x-request-id": "contains spaces" },
      }),
      context([]),
    );
    expect(response.status).toBe(404);
    const body = (await response.json()) as { request_id: string };
    expect(body.request_id).not.toContain("spaces");
    expect(response.headers.get("x-request-id")).toBe(body.request_id);
  });

  it.each([["."], [".."], ["projects", "..", "capabilities"], ["bad segment"]])(
    "rejects dot traversal and segments outside the strict path grammar: %j",
    async (...path) => {
      const fetchMock = vi.fn();
      vi.stubGlobal("fetch", fetchMock);
      const response = await GET(
        new Request("http://web/api/v1/invalid"),
        context(path),
      );
      expect(response.status).toBe(404);
      expect(fetchMock).not.toHaveBeenCalled();
    },
  );

  it("rejects declared oversized bodies before contacting upstream", async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    const response = await POST(
      new Request("http://web/api/v1/evaluations", {
        method: "POST",
        headers: {
          "content-length": String(10 * 1024 * 1024 + 1),
          ...authenticatedHeaders,
        },
        body: "small",
      }),
      context(["evaluations"]),
    );
    expect(response.status).toBe(413);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("streams and cancels a chunked oversized request before contacting upstream", async () => {
    let cancelled = false;
    const stream = new ReadableStream<Uint8Array>({
      start(controller) {
        controller.enqueue(new Uint8Array(6 * 1024 * 1024));
        controller.enqueue(new Uint8Array(6 * 1024 * 1024));
      },
      cancel() {
        cancelled = true;
      },
    });
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    const response = await POST(
      new Request("http://web/api/v1/evaluations", {
        method: "POST",
        headers: authenticatedHeaders,
        body: stream,
        duplex: "half",
      } as RequestInit & { duplex: "half" }),
      context(["evaluations"]),
    );
    expect(response.status).toBe(413);
    expect(cancelled).toBe(true);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it.each(["-1", "not-a-number", "01"])(
    "rejects an invalid request Content-Length: %s",
    async (contentLength) => {
      const fetchMock = vi.fn();
      vi.stubGlobal("fetch", fetchMock);
      const response = await POST(
        new Request("http://web/api/v1/evaluations", {
          method: "POST",
          headers: { ...authenticatedHeaders, "content-length": contentLength },
          body: "{}",
        }),
        context(["evaluations"]),
      );
      expect(response.status).toBe(413);
      expect(fetchMock).not.toHaveBeenCalled();
    },
  );

  it("rejects oversized query strings before contacting upstream", async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    const response = await GET(
      new Request(`http://web/api/v1/inventories?query=${"x".repeat(4_096)}`),
      context(["inventories"]),
    );

    expect(response.status).toBe(414);
    expect(await response.json()).toMatchObject({
      code: "request_uri_too_long",
    });
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("synthesizes bearer authorization from the server session and enforces CSRF", async () => {
    const now = Date.now();
    const store = new MemorySessionStore(() => now);
    const sessionId = Buffer.alloc(32, 31).toString("base64url");
    const csrf = Buffer.alloc(32, 32).toString("base64url");
    const session: BrowserSession = {
      version: 1,
      mode: "oidc",
      subject: "user-1",
      accessToken: "server-held-access-token",
      refreshToken: "server-held-refresh-token",
      idToken: "server-held-id-token",
      accessExpiresAt: now + 300_000,
      csrfToken: csrf,
      createdAt: now,
      absoluteExpiresAt: now + 3_600_000,
      idleExpiresAt: now + 600_000,
    };
    await store.saveSession(sessionId, session, 600_000);
    const provider = {
      authorizationUrl: vi.fn(),
      exchange: vi.fn(),
      refresh: vi.fn(),
      revoke: vi.fn(),
    };
    setAuthRuntimeForTests({
      service: new AuthService(authConfig("oidc"), store, provider, () => now),
    });
    const fetchMock = vi.fn().mockResolvedValue(Response.json({ ok: true }));
    vi.stubGlobal("fetch", fetchMock);

    const rejected = await POST(
      new Request("https://web.example/api/v1/evaluations", {
        method: "POST",
        headers: { cookie: `__Host-corpuskit_session=${sessionId}` },
        body: "{}",
      }),
      context(["evaluations"]),
    );
    expect(rejected.status).toBe(403);
    expect(fetchMock).not.toHaveBeenCalled();

    const accepted = await POST(
      new Request("https://web.example/api/v1/evaluations", {
        method: "POST",
        headers: {
          authorization: "Bearer browser-poison",
          cookie: `__Host-corpuskit_session=${sessionId}`,
          "x-corpuskit-csrf": csrf,
        },
        body: "{}",
      }),
      context(["evaluations"]),
    );
    expect(accepted.status).toBe(200);
    const forwarded = new Headers(fetchMock.mock.calls[0]?.[1]?.headers);
    expect(forwarded.get("authorization")).toBe(
      "Bearer server-held-access-token",
    );
    expect(JSON.stringify(await accepted.json())).not.toContain(
      "server-held-access-token",
    );
  });

  it("fails closed when a rotated or missing session ID is presented", async () => {
    setAuthRuntimeForTests({
      service: new AuthService(authConfig("oidc"), new MemorySessionStore(), {
        authorizationUrl: vi.fn(),
        exchange: vi.fn(),
        refresh: vi.fn(),
        revoke: vi.fn(),
      }),
    });
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    const response = await GET(
      new Request("https://web.example/api/v1/projects", {
        headers: {
          cookie: `__Host-corpuskit_session=${Buffer.alloc(32, 99).toString("base64url")}`,
        },
      }),
      context(["projects"]),
    );
    expect(response.status).toBe(401);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("caps oversized upstream responses without forwarding provider details", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response("too large", {
          headers: { "content-length": String(10 * 1024 * 1024 + 1) },
        }),
      ),
    );
    const response = await GET(
      new Request("https://web.example/api/v1/projects"),
      context(["projects"]),
    );
    expect(response.status).toBe(502);
    expect(await response.json()).toMatchObject({
      code: "api_response_too_large",
    });
  });
});
