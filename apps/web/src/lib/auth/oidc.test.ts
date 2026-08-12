import { beforeEach, describe, expect, it, vi } from "vitest";

const oidcMocks = vi.hoisted(() => ({
  authorizationCodeGrant: vi.fn(),
  buildAuthorizationUrl: vi.fn(),
  calculatePKCECodeChallenge: vi.fn(),
  clientSecretBasic: vi.fn(),
  customFetch: Symbol("customFetch"),
  discovery: vi.fn(),
  refreshTokenGrant: vi.fn(),
  tokenRevocation: vi.fn(),
}));

vi.mock("openid-client", () => ({
  authorizationCodeGrant: oidcMocks.authorizationCodeGrant,
  buildAuthorizationUrl: oidcMocks.buildAuthorizationUrl,
  calculatePKCECodeChallenge: oidcMocks.calculatePKCECodeChallenge,
  ClientSecretBasic: oidcMocks.clientSecretBasic,
  customFetch: oidcMocks.customFetch,
  discovery: oidcMocks.discovery,
  refreshTokenGrant: oidcMocks.refreshTokenGrant,
  tokenRevocation: oidcMocks.tokenRevocation,
}));

import { OpenIdClientProvider } from "./oidc";
import type { WebAuthConfig } from "./types";

const issuer = new URL("https://identity.example/realms/corpuskit");
const metadata = {
  issuer: issuer.href,
  authorization_endpoint: "https://identity.example/authorize",
  jwks_uri: "https://identity.example/jwks",
  revocation_endpoint: "https://identity.example/revoke",
  token_endpoint: "https://identity.example/token",
};
const configuration = { serverMetadata: () => metadata };

function config(): WebAuthConfig {
  return {
    mode: "oidc",
    environment: "production",
    issuer,
    audience: "corpuskit-api",
    clientId: "corpuskit-web",
    clientSecret: "a-production-client-secret",
    redirectUri: new URL("https://web.example/auth/callback"),
    stateSecret: new Uint8Array(Buffer.alloc(32, 1)),
    sessionEncryptionKeys: [new Uint8Array(Buffer.alloc(32, 2))],
    scopes: "openid profile offline_access",
    allowedReturnPaths: new Set(["/"]),
    sessionAbsoluteSeconds: 3_600,
    sessionIdleSeconds: 600,
    refreshBeforeSeconds: 60,
    transactionSeconds: 600,
    oidcTimeoutSeconds: 5,
    redisUrl: new URL("rediss://user:password@cache.example/0"),
    redisCommandTimeoutMs: 1_000,
    redisPrefix: "test:web",
  };
}

function tokenSet(overrides: Record<string, unknown> = {}) {
  return {
    access_token: "access-secret",
    refresh_token: "refresh-secret",
    id_token: "identity-secret",
    token_type: "Bearer",
    expiresIn: () => 300,
    claims: () => ({
      iss: issuer.href,
      aud: "corpuskit-web",
      sub: "user-1",
      name: "User One",
    }),
    ...overrides,
  };
}

const exchangeInput = {
  callbackUrl: new URL(
    "https://web.example/auth/callback?code=one&state=state",
  ),
  state: "state",
  nonce: "nonce",
  codeVerifier: "verifier",
} as const;

function invokeCustomFetch(
  rawUrl: string,
  redirect: RequestRedirect = "manual",
) {
  oidcMocks.discovery.mockImplementationOnce(
    async (
      _issuer: URL,
      _clientId: string,
      _metadata: unknown,
      _authentication: unknown,
      options: Record<PropertyKey, unknown>,
    ) => {
      const customFetch = options[oidcMocks.customFetch] as typeof fetch;
      await customFetch(new URL(rawUrl), { redirect });
      return configuration;
    },
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  oidcMocks.clientSecretBasic.mockReturnValue(vi.fn());
  oidcMocks.calculatePKCECodeChallenge.mockResolvedValue("pkce-challenge");
  oidcMocks.buildAuthorizationUrl.mockImplementation(
    (_configuration, parameters: Record<string, string>) => {
      const url = new URL(metadata.authorization_endpoint);
      for (const [name, value] of Object.entries(parameters)) {
        url.searchParams.set(name, value);
      }
      return url;
    },
  );
  oidcMocks.discovery.mockResolvedValue(configuration);
  oidcMocks.authorizationCodeGrant.mockResolvedValue(tokenSet());
  oidcMocks.refreshTokenGrant.mockResolvedValue(
    tokenSet({
      access_token: "rotated-access",
      refresh_token: "rotated-refresh",
      id_token: undefined,
    }),
  );
  oidcMocks.tokenRevocation.mockResolvedValue(undefined);
});

describe("OpenIdClientProvider", () => {
  it("uses Authorization Code + S256 PKCE and binds state and nonce on exchange", async () => {
    const now = 1_700_000_000_000;
    const provider = new OpenIdClientProvider(config(), () => now);
    const authorizationUrl = await provider.authorizationUrl({
      state: "signed-state",
      nonce: "expected-nonce",
      codeVerifier: "pkce-verifier",
    });
    expect(authorizationUrl.searchParams.get("code_challenge_method")).toBe(
      "S256",
    );
    expect(authorizationUrl.searchParams.get("state")).toBe("signed-state");
    expect(authorizationUrl.searchParams.get("nonce")).toBe("expected-nonce");
    expect(oidcMocks.calculatePKCECodeChallenge).toHaveBeenCalledWith(
      "pkce-verifier",
    );

    const callbackUrl = new URL(
      "https://web.example/auth/callback?code=one&state=signed-state",
    );
    await expect(
      provider.exchange({
        callbackUrl,
        state: "signed-state",
        nonce: "expected-nonce",
        codeVerifier: "pkce-verifier",
      }),
    ).resolves.toEqual({
      subject: "user-1",
      displayName: "User One",
      accessToken: "access-secret",
      refreshToken: "refresh-secret",
      idToken: "identity-secret",
      expiresAt: now + 300_000,
    });
    expect(oidcMocks.authorizationCodeGrant).toHaveBeenCalledWith(
      configuration,
      callbackUrl,
      expect.objectContaining({
        expectedNonce: "expected-nonce",
        expectedState: "signed-state",
        idTokenExpected: true,
        pkceCodeVerifier: "pkce-verifier",
      }),
    );
  });

  it("refreshes and revokes server-held tokens through the discovered provider", async () => {
    const now = 1_700_000_000_000;
    const provider = new OpenIdClientProvider(config(), () => now);
    await expect(provider.refresh("refresh-secret")).resolves.toMatchObject({
      accessToken: "rotated-access",
      refreshToken: "rotated-refresh",
      expiresAt: now + 300_000,
    });
    await provider.revoke("refresh-secret", "refresh_token");
    expect(oidcMocks.refreshTokenGrant).toHaveBeenCalledWith(
      configuration,
      "refresh-secret",
    );
    expect(oidcMocks.tokenRevocation).toHaveBeenCalledWith(
      configuration,
      "refresh-secret",
      { token_type_hint: "refresh_token" },
    );
  });

  it.each([
    ["issuer", { iss: "https://other.example" }],
    ["audience", { aud: "other-client" }],
    ["trimmed subject", { sub: " user-1" }],
    ["control subject", { sub: "user\n1" }],
  ])("rejects invalid %s claims", async (_label, claimOverride) => {
    oidcMocks.authorizationCodeGrant.mockResolvedValueOnce(
      tokenSet({
        claims: () => ({
          iss: issuer.href,
          aud: "corpuskit-web",
          sub: "user-1",
          ...claimOverride,
        }),
      }),
    );
    const provider = new OpenIdClientProvider(config());
    await expect(
      provider.exchange({
        callbackUrl: new URL(
          "https://web.example/auth/callback?code=one&state=state",
        ),
        state: "state",
        nonce: "nonce",
        codeVerifier: "verifier",
      }),
    ).rejects.toThrow("OIDC identity claims are invalid.");
  });

  it("rejects metadata endpoints with another origin, port, query, credentials, or fragment", async () => {
    for (const value of [
      "https://other.example/token",
      "https://identity.example:8443/token",
      "https://identity.example/token?tenant=other",
      "https://user:password@identity.example/token",
      "https://identity.example/token#fragment",
    ]) {
      oidcMocks.discovery.mockResolvedValueOnce({
        serverMetadata: () => ({ ...metadata, token_endpoint: value }),
      });
      await expect(
        new OpenIdClientProvider(config()).authorizationUrl({
          state: "state",
          nonce: "nonce",
          codeVerifier: "verifier",
        }),
      ).rejects.toThrow("OIDC provider endpoint is invalid.");
    }
  });

  it("streams provider bodies into a hard cap and cancels oversized chunked responses", async () => {
    let cancelCalled = false;
    const body = new ReadableStream<Uint8Array>({
      start(controller) {
        controller.enqueue(new Uint8Array(700_000));
        controller.enqueue(new Uint8Array(700_000));
      },
      cancel() {
        cancelCalled = true;
      },
    });
    const networkFetch = vi.fn().mockResolvedValue(new Response(body));
    oidcMocks.discovery.mockImplementationOnce(
      async (
        _issuer: URL,
        _clientId: string,
        _metadata: unknown,
        _authentication: unknown,
        options: Record<PropertyKey, unknown>,
      ) => {
        const customFetch = options[oidcMocks.customFetch] as typeof fetch;
        await customFetch(
          new URL("https://identity.example/.well-known/openid-configuration"),
          { redirect: "manual" },
        );
        return configuration;
      },
    );
    await expect(
      new OpenIdClientProvider(
        config(),
        Date.now,
        networkFetch,
      ).authorizationUrl({
        state: "state",
        nonce: "nonce",
        codeVerifier: "verifier",
      }),
    ).rejects.toThrow("OIDC response is too large.");
    expect(cancelCalled).toBe(true);
  });

  it("rejects provider redirects and never logs token or endpoint details", async () => {
    const consoleSpy = vi
      .spyOn(console, "error")
      .mockImplementation(() => undefined);
    const networkFetch = vi.fn().mockResolvedValue(
      new Response(null, {
        status: 302,
        headers: { location: "https://evil.example/provider-secret" },
      }),
    );
    oidcMocks.discovery.mockImplementationOnce(
      async (
        _issuer: URL,
        _clientId: string,
        _metadata: unknown,
        _authentication: unknown,
        options: Record<PropertyKey, unknown>,
      ) => {
        const customFetch = options[oidcMocks.customFetch] as typeof fetch;
        await customFetch(new URL("https://identity.example/discovery"), {
          redirect: "manual",
        });
        return configuration;
      },
    );
    await expect(
      new OpenIdClientProvider(
        config(),
        Date.now,
        networkFetch,
      ).authorizationUrl({
        state: "state",
        nonce: "nonce",
        codeVerifier: "verifier",
      }),
    ).rejects.toThrow("OIDC redirects are disabled.");
    expect(consoleSpy).not.toHaveBeenCalled();
    consoleSpy.mockRestore();
  });

  it("rejects incomplete provider construction and unsafe authorization or callback URLs", async () => {
    expect(
      () =>
        new OpenIdClientProvider({
          ...config(),
          clientSecret: undefined,
        }),
    ).toThrow("OIDC configuration is incomplete.");

    oidcMocks.buildAuthorizationUrl.mockReturnValueOnce(
      new URL("https://evil.example/authorize"),
    );
    await expect(
      new OpenIdClientProvider(config()).authorizationUrl({
        state: "state",
        nonce: "nonce",
        codeVerifier: "verifier",
      }),
    ).rejects.toThrow("OIDC authorization endpoint is invalid.");

    oidcMocks.buildAuthorizationUrl.mockReturnValueOnce(
      new URL(`https://identity.example/authorize?x=${"a".repeat(4_096)}`),
    );
    await expect(
      new OpenIdClientProvider(config()).authorizationUrl({
        state: "state",
        nonce: "nonce",
        codeVerifier: "verifier",
      }),
    ).rejects.toThrow("OIDC authorization endpoint is invalid.");

    const provider = new OpenIdClientProvider(config());
    await expect(
      provider.exchange({
        ...exchangeInput,
        callbackUrl: new URL("https://web.example/auth/callback#fragment"),
      }),
    ).rejects.toThrow("OIDC callback is invalid.");
    await expect(
      provider.exchange({
        ...exchangeInput,
        callbackUrl: new URL(
          `https://web.example/auth/callback?x=${"a".repeat(4_096)}`,
        ),
      }),
    ).rejects.toThrow("OIDC callback is invalid.");
  });

  it.each([
    [
      "missing claims",
      { claims: () => undefined },
      "OIDC ID token is missing.",
    ],
    [
      "access token",
      { access_token: "bad token" },
      "OIDC token response is invalid.",
    ],
    [
      "refresh token",
      { refresh_token: "bad token" },
      "OIDC token response is invalid.",
    ],
    ["ID token", { id_token: "bad token" }, "OIDC token response is invalid."],
    ["token type", { token_type: "DPoP" }, "OIDC token response is invalid."],
    [
      "missing expiry",
      { expiresIn: () => undefined },
      "OIDC token expiry is invalid.",
    ],
    ["short expiry", { expiresIn: () => 29 }, "OIDC token expiry is invalid."],
    [
      "long expiry",
      { expiresIn: () => 86_401 },
      "OIDC token expiry is invalid.",
    ],
  ])("rejects an invalid exchange %s", async (_label, override, message) => {
    oidcMocks.authorizationCodeGrant.mockResolvedValueOnce(tokenSet(override));
    await expect(
      new OpenIdClientProvider(config()).exchange(exchangeInput),
    ).rejects.toThrow(message);
  });

  it.each([
    ["input", "bad token", undefined],
    ["access", "refresh-secret", { access_token: "bad token" }],
    ["rotated refresh", "refresh-secret", { refresh_token: "bad token" }],
    ["ID token", "refresh-secret", { id_token: "bad token" }],
    ["type", "refresh-secret", { token_type: "DPoP" }],
  ])("rejects an invalid refresh %s", async (_label, input, override) => {
    if (override) {
      oidcMocks.refreshTokenGrant.mockResolvedValueOnce(tokenSet(override));
    }
    await expect(
      new OpenIdClientProvider(config()).refresh(input),
    ).rejects.toThrow();
  });

  it("accepts array audiences and optional refresh claims while rejecting malformed arrays", async () => {
    oidcMocks.authorizationCodeGrant.mockResolvedValueOnce(
      tokenSet({
        claims: () => ({
          iss: issuer.href,
          aud: ["other", "corpuskit-web"],
          sub: "user-1",
        }),
      }),
    );
    await expect(
      new OpenIdClientProvider(config()).exchange(exchangeInput),
    ).resolves.toMatchObject({ subject: "user-1" });

    for (const aud of [[], [1, "corpuskit-web"], ["other"]]) {
      oidcMocks.authorizationCodeGrant.mockResolvedValueOnce(
        tokenSet({
          claims: () => ({ iss: issuer.href, aud, sub: "user-1" }),
        }),
      );
      await expect(
        new OpenIdClientProvider(config()).exchange(exchangeInput),
      ).rejects.toThrow("OIDC identity claims are invalid.");
    }

    oidcMocks.refreshTokenGrant.mockResolvedValueOnce(
      tokenSet({ claims: () => undefined, refresh_token: undefined }),
    );
    await expect(
      new OpenIdClientProvider(config()).refresh("refresh-secret"),
    ).resolves.toMatchObject({ subject: undefined, refreshToken: undefined });
  });

  it("omits untrusted display names without rejecting the identity", async () => {
    for (const name of ["", " padded ", "control\nname", "x".repeat(161)]) {
      oidcMocks.authorizationCodeGrant.mockResolvedValueOnce(
        tokenSet({
          claims: () => ({
            iss: issuer.href,
            aud: "corpuskit-web",
            sub: "user-1",
            name,
          }),
        }),
      );
      await expect(
        new OpenIdClientProvider(config()).exchange(exchangeInput),
      ).resolves.toMatchObject({ displayName: undefined });
    }
  });

  it.each([
    "http://identity.example/discovery",
    "https://other.example/discovery",
    "https://user:password@identity.example/discovery",
    "https://identity.example/discovery#fragment",
  ])("rejects an unsafe provider network target: %s", async (target) => {
    invokeCustomFetch(target);
    const networkFetch = vi.fn();
    await expect(
      new OpenIdClientProvider(
        config(),
        Date.now,
        networkFetch,
      ).authorizationUrl({
        state: "state",
        nonce: "nonce",
        codeVerifier: "verifier",
      }),
    ).rejects.toThrow("OIDC network target is invalid.");
    expect(networkFetch).not.toHaveBeenCalled();
  });

  it("rejects invalid provider response lengths and permits an empty bounded response", async () => {
    for (const contentLength of ["invalid", String(1_048_577)]) {
      invokeCustomFetch("https://identity.example/discovery");
      const networkFetch = vi
        .fn()
        .mockResolvedValue(
          new Response(null, { headers: { "content-length": contentLength } }),
        );
      await expect(
        new OpenIdClientProvider(
          config(),
          Date.now,
          networkFetch,
        ).authorizationUrl({
          state: "state",
          nonce: "nonce",
          codeVerifier: "verifier",
        }),
      ).rejects.toThrow();
    }

    invokeCustomFetch("https://identity.example/discovery");
    const networkFetch = vi.fn().mockResolvedValue(new Response(null));
    await expect(
      new OpenIdClientProvider(
        config(),
        Date.now,
        networkFetch,
      ).authorizationUrl({
        state: "state",
        nonce: "nonce",
        codeVerifier: "verifier",
      }),
    ).resolves.toBeInstanceOf(URL);
  });

  it("rejects incomplete or mismatched metadata and retries discovery after failure", async () => {
    oidcMocks.discovery.mockResolvedValueOnce({
      serverMetadata: () => ({ ...metadata, issuer: "https://other.example" }),
    });
    const provider = new OpenIdClientProvider(config());
    await expect(
      provider.authorizationUrl({
        state: "state",
        nonce: "nonce",
        codeVerifier: "verifier",
      }),
    ).rejects.toThrow("OIDC issuer metadata is invalid.");

    oidcMocks.discovery.mockResolvedValueOnce({
      serverMetadata: () => ({ ...metadata, jwks_uri: undefined }),
    });
    await expect(
      provider.authorizationUrl({
        state: "state",
        nonce: "nonce",
        codeVerifier: "verifier",
      }),
    ).rejects.toThrow("OIDC provider metadata is incomplete.");

    oidcMocks.discovery.mockRejectedValueOnce(new Error("temporary outage"));
    await expect(
      provider.authorizationUrl({
        state: "state",
        nonce: "nonce",
        codeVerifier: "verifier",
      }),
    ).rejects.toThrow("temporary outage");
    await expect(
      provider.authorizationUrl({
        state: "state",
        nonce: "nonce",
        codeVerifier: "verifier",
      }),
    ).resolves.toBeInstanceOf(URL);
  });

  it("rejects malformed revocation input before provider contact", async () => {
    await expect(
      new OpenIdClientProvider(config()).revoke("bad token", "access_token"),
    ).rejects.toThrow("Token is invalid.");
    expect(oidcMocks.tokenRevocation).not.toHaveBeenCalled();
  });
});
