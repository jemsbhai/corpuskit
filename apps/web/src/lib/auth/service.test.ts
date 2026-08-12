import { describe, expect, it, vi } from "vitest";

import { AuthService } from "./service";
import { MemorySessionStore } from "./session-store";
import type {
  BrowserSession,
  OidcProvider,
  SessionStore,
  WebAuthConfig,
} from "./types";
import { AuthError } from "./types";

function config(mode: "demo" | "oidc" | "test" = "oidc"): WebAuthConfig {
  return {
    mode,
    environment: mode === "test" ? "test" : "development",
    ...(mode === "oidc"
      ? {
          issuer: new URL("https://identity.example"),
          audience: "corpuskit-api",
          clientId: "corpuskit-web",
          clientSecret: "secret-value-long-enough",
          redirectUri: new URL("https://web.example/auth/callback"),
        }
      : {}),
    stateSecret: new Uint8Array(Buffer.alloc(32, 9)),
    sessionEncryptionKeys: [new Uint8Array(Buffer.alloc(32, 10))],
    scopes: mode === "oidc" ? "openid profile offline_access" : "",
    allowedReturnPaths: new Set(["/", "/projects"]),
    sessionAbsoluteSeconds: 3_600,
    sessionIdleSeconds: 600,
    refreshBeforeSeconds: 60,
    transactionSeconds: 600,
    oidcTimeoutSeconds: 5,
    redisCommandTimeoutMs: 2_000,
    redisPrefix: "test:web",
  };
}

function randomSource() {
  let value = 0;
  return {
    bytes(size: number) {
      value += 1;
      return new Uint8Array(Buffer.alloc(size, value));
    },
  };
}

function provider(): OidcProvider & {
  authorizationUrl: ReturnType<typeof vi.fn>;
  exchange: ReturnType<typeof vi.fn>;
  refresh: ReturnType<typeof vi.fn>;
  revoke: ReturnType<typeof vi.fn>;
} {
  return {
    authorizationUrl: vi.fn(async (input: { state: string }) => {
      const url = new URL("https://identity.example/authorize");
      url.searchParams.set("state", input.state);
      return url;
    }),
    exchange: vi.fn(async () => ({
      subject: "user-1",
      displayName: "User One",
      accessToken: "access-secret",
      refreshToken: "refresh-secret",
      idToken: "identity-secret",
      expiresAt: 1_700_000_300_000,
    })),
    refresh: vi.fn(async () => ({
      subject: "user-1",
      accessToken: "rotated-access-secret",
      refreshToken: "rotated-refresh-secret",
      expiresAt: 1_700_000_600_000,
    })),
    revoke: vi.fn(async () => undefined),
  };
}

function oidcSession(overrides: Partial<BrowserSession> = {}): BrowserSession {
  return {
    version: 1,
    mode: "oidc",
    subject: "user-1",
    accessToken: "old-access-secret",
    refreshToken: "old-refresh-secret",
    idToken: "old-id-secret",
    accessExpiresAt: 1_700_000_010_000,
    csrfToken: Buffer.alloc(32, 20).toString("base64url"),
    createdAt: 1_699_999_000_000,
    absoluteExpiresAt: 1_700_003_600_000,
    idleExpiresAt: 1_700_000_600_000,
    ...overrides,
  };
}

describe("AuthService login", () => {
  it("enforces provider/mode invariants and refuses login in local modes", async () => {
    const store = new MemorySessionStore();
    expect(() => new AuthService(config(), store)).toThrow(TypeError);
    expect(() => new AuthService(config("demo"), store, provider())).toThrow(
      TypeError,
    );
    const local = new AuthService(config("demo"), store);
    await expect(local.beginLogin("/")).rejects.toMatchObject({
      code: "authentication_unavailable",
    });
    await expect(
      local.completeLogin(
        new URL("https://web.example/auth/callback"),
        null,
        null,
      ),
    ).rejects.toMatchObject({ code: "authentication_unavailable" });
    await expect(
      new AuthService(
        config(),
        new MemorySessionStore(),
        provider(),
      ).bootstrapLocalSession(),
    ).rejects.toMatchObject({ code: "authentication_required" });
  });

  it("fails closed after identifier collisions or authorization endpoint failure", async () => {
    const store = new MemorySessionStore();
    vi.spyOn(store, "createTransaction").mockResolvedValue(false);
    await expect(
      new AuthService(config(), store, provider()).beginLogin("/"),
    ).rejects.toMatchObject({ code: "authentication_unavailable" });
    expect(store.createTransaction).toHaveBeenCalledTimes(3);

    const unavailable = provider();
    unavailable.authorizationUrl.mockRejectedValueOnce(new Error("outage"));
    await expect(
      new AuthService(
        config(),
        new MemorySessionStore(),
        unavailable,
      ).beginLogin("/"),
    ).rejects.toMatchObject({ code: "authentication_unavailable" });
  });

  it("binds state, nonce, PKCE, cookie correlation, and exact return path", async () => {
    const now = 1_700_000_000_000;
    const store = new MemorySessionStore(() => now);
    const oidc = provider();
    const service = new AuthService(
      config(),
      store,
      oidc,
      () => now,
      randomSource(),
    );
    await expect(
      service.beginLogin("https://evil.example"),
    ).rejects.toMatchObject({
      code: "invalid_return_path",
    });

    const start = await service.beginLogin("/projects");
    const state = start.authorizationUrl.searchParams.get("state");
    expect(state).toBeTruthy();
    expect(oidc.authorizationUrl).toHaveBeenCalledWith(
      expect.objectContaining({
        state,
        nonce: expect.stringMatching(/^[A-Za-z0-9_-]{43}$/u),
        codeVerifier: expect.stringMatching(/^[A-Za-z0-9_-]{43}$/u),
      }),
    );

    await expect(
      service.completeLogin(
        new URL(`https://web.example/auth/callback?code=one&state=${state}`),
        Buffer.alloc(32, 99).toString("base64url"),
        null,
      ),
    ).rejects.toMatchObject({ code: "invalid_authentication_callback" });

    const callback = new URL(
      `https://web.example/auth/callback?code=one&state=${encodeURIComponent(state ?? "")}`,
    );
    const result = await service.completeLogin(
      callback,
      start.correlationId,
      null,
    );
    expect(result.returnPath).toBe("/projects");
    expect(result.sessionId).not.toBe(start.correlationId);
    expect(oidc.exchange).toHaveBeenCalledWith(
      expect.objectContaining({
        callbackUrl: callback,
        state,
        nonce: expect.any(String),
        codeVerifier: expect.any(String),
      }),
    );
    const saved = await store.getSession(result.sessionId);
    expect(saved?.accessToken).toBe("access-secret");
    const exposed = JSON.stringify(service.publicSession(saved!));
    expect(exposed).not.toContain("access-secret");
    expect(exposed).not.toContain("refresh-secret");
    expect(exposed).not.toContain("identity-secret");

    await expect(
      service.completeLogin(callback, start.correlationId, null),
    ).rejects.toMatchObject({ code: "invalid_authentication_callback" });
  });

  it("rejects tampered state before exchanging a code", async () => {
    const now = 1_700_000_000_000;
    const oidc = provider();
    const service = new AuthService(
      config(),
      new MemorySessionStore(() => now),
      oidc,
      () => now,
      randomSource(),
    );
    const start = await service.beginLogin("/");
    const state = start.authorizationUrl.searchParams.get("state") ?? "";
    await expect(
      service.completeLogin(
        new URL(
          `https://web.example/auth/callback?code=one&state=${encodeURIComponent(`${state}x`)}`,
        ),
        start.correlationId,
        null,
      ),
    ).rejects.toBeInstanceOf(AuthError);
    expect(oidc.exchange).not.toHaveBeenCalled();
  });

  it("rejects missing, duplicate, and provider-rejected callbacks", async () => {
    const now = 1_700_000_000_000;
    const oidc = provider();
    const service = new AuthService(
      config(),
      new MemorySessionStore(() => now),
      oidc,
      () => now,
      randomSource(),
    );
    await expect(
      service.completeLogin(
        new URL("https://web.example/auth/callback"),
        null,
        null,
      ),
    ).rejects.toMatchObject({ code: "invalid_authentication_callback" });

    const first = await service.beginLogin("/");
    const firstState = first.authorizationUrl.searchParams.get("state");
    await expect(
      service.completeLogin(
        new URL(
          `https://web.example/auth/callback?state=${firstState}&state=${firstState}`,
        ),
        first.correlationId,
        null,
      ),
    ).rejects.toMatchObject({ code: "invalid_authentication_callback" });

    const second = await service.beginLogin("/");
    const secondState = second.authorizationUrl.searchParams.get("state");
    oidc.exchange.mockRejectedValueOnce(new Error("token endpoint rejected"));
    await expect(
      service.completeLogin(
        new URL(
          `https://web.example/auth/callback?code=x&state=${secondState}`,
        ),
        second.correlationId,
        null,
      ),
    ).rejects.toMatchObject({ code: "invalid_authentication_callback" });
  });

  it("refuses a fixed replacement session identifier", async () => {
    const now = 1_700_000_000_000;
    const fixed = Buffer.alloc(32, 44).toString("base64url");
    const fixedRandom = {
      bytes: (size: number) => new Uint8Array(Buffer.alloc(size, 44)),
    };
    const service = new AuthService(
      config(),
      new MemorySessionStore(() => now),
      provider(),
      () => now,
      fixedRandom,
    );
    const started = await service.beginLogin("/");
    const state = started.authorizationUrl.searchParams.get("state");
    expect(started.correlationId).toBe(fixed);
    await expect(
      service.completeLogin(
        new URL(`https://web.example/auth/callback?code=x&state=${state}`),
        fixed,
        fixed,
      ),
    ).rejects.toMatchObject({ code: "authentication_unavailable" });
  });

  it("creates a valid session when the provider omits an optional display name", async () => {
    const now = 1_700_000_000_000;
    const store = new MemorySessionStore(() => now);
    const oidc = provider();
    oidc.exchange.mockResolvedValueOnce({
      subject: "user-1",
      accessToken: "access-secret",
      refreshToken: "refresh-secret",
      idToken: "identity-secret",
      expiresAt: now + 300_000,
    });
    const service = new AuthService(
      config(),
      store,
      oidc,
      () => now,
      randomSource(),
    );
    const started = await service.beginLogin("/");
    const state = started.authorizationUrl.searchParams.get("state");
    const completed = await service.completeLogin(
      new URL(`https://web.example/auth/callback?code=x&state=${state}`),
      started.correlationId,
      null,
    );
    await expect(
      store.getSession(completed.sessionId),
    ).resolves.not.toHaveProperty("displayName");
  });

  it("invalidates a previous session before issuing a non-fixed ID", async () => {
    const now = 1_700_000_000_000;
    const store = new MemorySessionStore(() => now);
    const previousId = Buffer.alloc(32, 33).toString("base64url");
    await store.saveSession(previousId, oidcSession(), 600_000);
    const service = new AuthService(
      config(),
      store,
      provider(),
      () => now,
      randomSource(),
    );
    const start = await service.beginLogin("/");
    const state = start.authorizationUrl.searchParams.get("state");
    const result = await service.completeLogin(
      new URL(`https://web.example/auth/callback?code=ok&state=${state}`),
      start.correlationId,
      previousId,
    );
    expect(result.sessionId).not.toBe(previousId);
    await expect(store.getSession(previousId)).resolves.toBeNull();
  });
});

describe("AuthService session lifecycle", () => {
  it("returns null for absent, missing, wrong-mode, and idle-expired sessions", async () => {
    const now = 1_700_000_000_000;
    const store = new MemorySessionStore(() => now);
    const oidc = provider();
    const service = new AuthService(config(), store, oidc, () => now);
    await expect(service.resolveSession(null)).resolves.toBeNull();
    await expect(
      service.resolveSession(Buffer.alloc(32, 80).toString("base64url")),
    ).resolves.toBeNull();

    const wrongModeId = Buffer.alloc(32, 81).toString("base64url");
    await store.saveSession(
      wrongModeId,
      {
        ...oidcSession(),
        mode: "demo",
        accessToken: undefined,
        refreshToken: undefined,
        idToken: undefined,
        accessExpiresAt: undefined,
      },
      600_000,
    );
    await expect(service.resolveSession(wrongModeId)).resolves.toBeNull();

    const expiredId = Buffer.alloc(32, 82).toString("base64url");
    await store.saveSession(
      expiredId,
      oidcSession({ idleExpiresAt: now }),
      600_000,
    );
    await expect(service.resolveSession(expiredId)).resolves.toBeNull();
    await expect(store.getSession(expiredId)).resolves.toBeNull();
  });

  it("does not refresh a sufficiently fresh token and supports explicit test identity", async () => {
    const now = 1_700_000_000_000;
    const store = new MemorySessionStore(() => now);
    const id = Buffer.alloc(32, 83).toString("base64url");
    await store.saveSession(
      id,
      oidcSession({ accessExpiresAt: now + 300_000 }),
      600_000,
    );
    const oidc = provider();
    const service = new AuthService(config(), store, oidc, () => now);
    await expect(service.resolveSession(id)).resolves.toMatchObject({ id });
    expect(oidc.refresh).not.toHaveBeenCalled();

    const testService = new AuthService(
      config("test"),
      new MemorySessionStore(() => now),
      undefined,
      () => now,
      randomSource(),
    );
    const local = await testService.bootstrapLocalSession();
    expect(local.value.subject).toBe("deterministic-test-user");
    expect(
      testService.publicSession({ ...local.value, displayName: undefined }),
    ).not.toHaveProperty("displayName");
    expect(testService.csrfMatches(local.value, null)).toBe(false);
  });
  it("refreshes early, rotates refresh tokens, and extends only idle expiry", async () => {
    let now = 1_700_000_000_000;
    const store = new MemorySessionStore(() => now);
    const id = Buffer.alloc(32, 10).toString("base64url");
    const original = oidcSession();
    await store.saveSession(id, original, 600_000);
    const oidc = provider();
    const service = new AuthService(
      config(),
      store,
      oidc,
      () => now,
      randomSource(),
    );
    const resolved = await service.resolveSession(id);
    expect(resolved?.value.accessToken).toBe("rotated-access-secret");
    expect(resolved?.value.refreshToken).toBe("rotated-refresh-secret");
    expect(resolved?.value.idToken).toBe("old-id-secret");
    expect(resolved?.value.absoluteExpiresAt).toBe(original.absoluteExpiresAt);
    expect(oidc.refresh).toHaveBeenCalledWith("old-refresh-secret");

    now = original.absoluteExpiresAt;
    await expect(service.resolveSession(id)).resolves.toBeNull();
  });

  it("serializes concurrent refreshes so a stale request cannot overwrite rotation", async () => {
    const now = 1_700_000_000_000;
    const store = new MemorySessionStore(() => now);
    const id = Buffer.alloc(32, 11).toString("base64url");
    await store.saveSession(id, oidcSession(), 600_000);
    const oidc = provider();
    let release: (() => void) | undefined;
    oidc.refresh.mockImplementation(
      async () =>
        new Promise((resolve) => {
          release = () =>
            resolve({
              subject: "user-1",
              accessToken: "concurrent-access",
              refreshToken: "concurrent-refresh",
              expiresAt: now + 600_000,
            });
        }),
    );
    const service = new AuthService(
      config(),
      store,
      oidc,
      () => now,
      randomSource(),
    );
    const first = service.resolveSession(id);
    const second = service.resolveSession(id);
    await vi.waitFor(() => expect(oidc.refresh).toHaveBeenCalledOnce());
    release?.();
    const [firstResult, secondResult] = await Promise.all([first, second]);
    expect(oidc.refresh).toHaveBeenCalledOnce();
    expect(firstResult?.value.refreshToken).toBe("concurrent-refresh");
    expect(secondResult?.value.refreshToken).toBe("concurrent-refresh");
    await expect(store.getSession(id)).resolves.toMatchObject({
      accessToken: "concurrent-access",
      refreshToken: "concurrent-refresh",
    });
  });

  it("fails closed and removes the session when refresh fails or is impossible", async () => {
    const now = 1_700_000_000_000;
    const firstStore = new MemorySessionStore(() => now);
    const firstId = Buffer.alloc(32, 12).toString("base64url");
    await firstStore.saveSession(firstId, oidcSession(), 600_000);
    const failing = provider();
    failing.refresh.mockRejectedValueOnce(new Error("provider secret detail"));
    const first = new AuthService(
      config(),
      firstStore,
      failing,
      () => now,
      randomSource(),
    );
    await expect(first.resolveSession(firstId)).rejects.toMatchObject({
      code: "authentication_unavailable",
    });
    await expect(firstStore.getSession(firstId)).resolves.toBeNull();

    const secondStore = new MemorySessionStore(() => now);
    const secondId = Buffer.alloc(32, 13).toString("base64url");
    await secondStore.saveSession(
      secondId,
      oidcSession({ refreshToken: undefined }),
      600_000,
    );
    const second = new AuthService(
      config(),
      secondStore,
      provider(),
      () => now,
      randomSource(),
    );
    await expect(second.resolveSession(secondId)).rejects.toMatchObject({
      code: "authentication_required",
    });
    await expect(secondStore.getSession(secondId)).resolves.toBeNull();
  });

  it("fails closed on subject changes and preserves AuthError identity", async () => {
    const now = 1_700_000_000_000;
    for (const failure of [
      { subject: "other-user", accessToken: "x", expiresAt: now + 300_000 },
      new AuthError("authentication_required", 401),
    ]) {
      const store = new MemorySessionStore(() => now);
      const id = Buffer.alloc(
        32,
        typeof failure === "object" && failure instanceof AuthError ? 85 : 84,
      ).toString("base64url");
      await store.saveSession(id, oidcSession(), 600_000);
      const oidc = provider();
      if (failure instanceof AuthError)
        oidc.refresh.mockRejectedValueOnce(failure);
      else oidc.refresh.mockResolvedValueOnce(failure);
      const service = new AuthService(config(), store, oidc, () => now);
      await expect(service.resolveSession(id)).rejects.toBeInstanceOf(
        AuthError,
      );
      await expect(store.getSession(id)).resolves.toBeNull();
    }
  });

  it("keeps the old refresh and ID token when rotation omits replacements", async () => {
    const now = 1_700_000_000_000;
    const store = new MemorySessionStore(() => now);
    const id = Buffer.alloc(32, 86).toString("base64url");
    await store.saveSession(id, oidcSession(), 600_000);
    const oidc = provider();
    oidc.refresh.mockResolvedValueOnce({
      accessToken: "new-access",
      expiresAt: now + 300_000,
    });
    const service = new AuthService(config(), store, oidc, () => now);
    await expect(service.resolveSession(id)).resolves.toMatchObject({
      value: {
        refreshToken: "old-refresh-secret",
        idToken: "old-id-secret",
      },
    });
  });

  it("refreshes an OIDC session with a missing access expiry defensively", async () => {
    const now = 1_700_000_000_000;
    const store = new MemorySessionStore(() => now);
    const id = Buffer.alloc(32, 89).toString("base64url");
    await store.saveSession(
      id,
      oidcSession({ accessExpiresAt: undefined }),
      600_000,
    );
    const oidc = provider();
    const service = new AuthService(config(), store, oidc, () => now);
    await expect(service.resolveSession(id)).resolves.toMatchObject({
      value: { accessToken: "rotated-access-secret" },
    });
    expect(oidc.refresh).toHaveBeenCalledOnce();
  });

  it("creates explicit local sessions and enforces session-bound CSRF on logout", async () => {
    const now = 1_700_000_000_000;
    const store = new MemorySessionStore(() => now);
    const service = new AuthService(
      config("demo"),
      store,
      undefined,
      () => now,
      randomSource(),
    );
    const created = await service.bootstrapLocalSession();
    expect(created.value.subject).toBe("demo-user");
    expect(service.csrfMatches(created.value, created.value.csrfToken)).toBe(
      true,
    );
    expect(service.csrfMatches(created.value, "wrong")).toBe(false);
    await expect(service.logout(created.id, "wrong")).rejects.toMatchObject({
      code: "csrf_validation_failed",
    });
    await expect(store.getSession(created.id)).resolves.not.toBeNull();
    await service.logout(created.id, created.value.csrfToken);
    await expect(store.getSession(created.id)).resolves.toBeNull();
    await expect(service.logout(null, null)).rejects.toMatchObject({
      code: "authentication_required",
    });
    await service.close();
  });

  it("revokes both server-held token classes while always completing cleanup", async () => {
    const now = 1_700_000_000_000;
    const store = new MemorySessionStore(() => now);
    const id = Buffer.alloc(32, 17).toString("base64url");
    const value = oidcSession();
    await store.saveSession(id, value, 600_000);
    const oidc = provider();
    oidc.revoke.mockRejectedValueOnce(new Error("revocation outage"));
    const service = new AuthService(
      config(),
      store,
      oidc,
      () => now,
      randomSource(),
    );
    await service.logout(id, value.csrfToken);
    expect(oidc.revoke).toHaveBeenCalledWith(
      "old-refresh-secret",
      "refresh_token",
    );
    expect(oidc.revoke).toHaveBeenCalledWith(
      "old-access-secret",
      "access_token",
    );
    await expect(store.getSession(id)).resolves.toBeNull();
  });

  it("rejects logout for a vanished session and tolerates sessions without revocable tokens", async () => {
    const now = 1_700_000_000_000;
    const store = new MemorySessionStore(() => now);
    const oidc = provider();
    const service = new AuthService(config(), store, oidc, () => now);
    await expect(
      service.logout(Buffer.alloc(32, 87).toString("base64url"), "csrf"),
    ).rejects.toMatchObject({ code: "authentication_required" });

    const id = Buffer.alloc(32, 88).toString("base64url");
    const value = oidcSession({
      accessToken: undefined,
      refreshToken: undefined,
    });
    await store.saveSession(id, value, 600_000);
    await service.logout(id, value.csrfToken);
    expect(oidc.revoke).not.toHaveBeenCalled();
  });

  it("converts session-store outage to a generic fail-closed error without logging", async () => {
    const consoleSpy = vi
      .spyOn(console, "error")
      .mockImplementation(() => undefined);
    const broken: SessionStore = {
      createTransaction: vi.fn(async () => {
        throw new Error("redis://secret-host");
      }),
      consumeTransaction: vi.fn(async () => null),
      getSession: vi.fn(async () => {
        throw new Error("redis://secret-host");
      }),
      saveSession: vi.fn(async () => undefined),
      deleteSession: vi.fn(async () => undefined),
      withSessionLock: vi.fn(async (_id, operation) => operation()),
      close: vi.fn(async () => undefined),
    };
    const service = new AuthService(
      config(),
      broken,
      provider(),
      () => 1_700_000_000_000,
      randomSource(),
    );
    await expect(
      service.resolveSession(Buffer.alloc(32).toString("base64url")),
    ).rejects.toMatchObject({
      code: "authentication_unavailable",
      message: "authentication_unavailable",
    });
    await expect(service.beginLogin("/")).rejects.toMatchObject({
      code: "authentication_unavailable",
    });
    expect(consoleSpy).not.toHaveBeenCalled();
    consoleSpy.mockRestore();
  });
});
