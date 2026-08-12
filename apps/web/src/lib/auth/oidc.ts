import * as oidc from "openid-client";

import type {
  OidcProvider,
  OidcRefreshSet,
  OidcTokenSet,
  WebAuthConfig,
} from "./types";

const maximumProviderResponseBytes = 1024 * 1024;
const maximumAuthorizationUrlCharacters = 4_096;
const tokenPattern = /^[\u0021-\u007E]{1,65536}$/u;

function requiredConfig(config: WebAuthConfig) {
  if (
    config.mode !== "oidc" ||
    !config.issuer ||
    !config.audience ||
    !config.clientId ||
    !config.clientSecret ||
    !config.redirectUri
  ) {
    throw new TypeError("OIDC configuration is incomplete.");
  }
  return {
    issuer: config.issuer,
    audience: config.audience,
    clientId: config.clientId,
    clientSecret: config.clientSecret,
    redirectUri: config.redirectUri,
    scopes: config.scopes,
    timeout: config.oidcTimeoutSeconds,
  };
}

function validToken(value: unknown): value is string {
  return typeof value === "string" && tokenPattern.test(value);
}

function expiration(
  tokens: oidc.TokenEndpointResponseHelpers,
  now: number,
): number {
  const seconds = tokens.expiresIn();
  if (
    seconds === undefined ||
    !Number.isSafeInteger(seconds) ||
    seconds < 30 ||
    seconds > 86_400
  ) {
    throw new Error("OIDC token expiry is invalid.");
  }
  return now + seconds * 1_000;
}

function displayName(claims: Record<string, unknown>): string | undefined {
  const value = claims.name;
  if (
    typeof value !== "string" ||
    value.length === 0 ||
    value.length > 160 ||
    value !== value.trim() ||
    /[\u0000-\u001F\u007F]/u.test(value)
  ) {
    return undefined;
  }
  return value;
}

async function readBoundedResponse(response: Response): Promise<ArrayBuffer> {
  const rawLength = response.headers.get("content-length");
  if (rawLength !== null) {
    if (!/^[0-9]+$/u.test(rawLength)) {
      throw new Error("OIDC response length is invalid.");
    }
    const declared = Number(rawLength);
    if (
      !Number.isSafeInteger(declared) ||
      declared > maximumProviderResponseBytes
    ) {
      throw new Error("OIDC response is too large.");
    }
  }
  if (!response.body) return new ArrayBuffer(0);
  const reader = response.body.getReader();
  const chunks: Uint8Array[] = [];
  let total = 0;
  while (true) {
    const part = await reader.read();
    if (part.done) break;
    total += part.value.byteLength;
    if (total > maximumProviderResponseBytes) {
      try {
        await reader.cancel();
      } catch {
        // The response is rejected regardless of a transport cancellation error.
      }
      throw new Error("OIDC response is too large.");
    }
    chunks.push(part.value);
  }
  const bounded = new Uint8Array(total);
  let offset = 0;
  for (const chunk of chunks) {
    bounded.set(chunk, offset);
    offset += chunk.byteLength;
  }
  return bounded.buffer;
}

export class OpenIdClientProvider implements OidcProvider {
  private readonly values: ReturnType<typeof requiredConfig>;
  private configuration?: Promise<oidc.Configuration>;

  constructor(
    config: WebAuthConfig,
    private readonly now: () => number = Date.now,
    private readonly networkFetch: typeof fetch = fetch,
  ) {
    this.values = requiredConfig(config);
  }

  async authorizationUrl(input: {
    readonly state: string;
    readonly nonce: string;
    readonly codeVerifier: string;
  }): Promise<URL> {
    const configuration = await this.getConfiguration();
    const codeChallenge = await oidc.calculatePKCECodeChallenge(
      input.codeVerifier,
    );
    const url = oidc.buildAuthorizationUrl(configuration, {
      audience: this.values.audience,
      code_challenge: codeChallenge,
      code_challenge_method: "S256",
      nonce: input.nonce,
      redirect_uri: this.values.redirectUri.href,
      scope: this.values.scopes,
      state: input.state,
    });
    if (
      url.origin !== this.values.issuer.origin ||
      url.href.length > maximumAuthorizationUrlCharacters
    ) {
      throw new Error("OIDC authorization endpoint is invalid.");
    }
    return url;
  }

  async exchange(input: {
    readonly callbackUrl: URL;
    readonly state: string;
    readonly nonce: string;
    readonly codeVerifier: string;
  }): Promise<OidcTokenSet> {
    const configuration = await this.getConfiguration();
    const currentUrl = new URL(this.values.redirectUri);
    if (input.callbackUrl.hash || input.callbackUrl.search.length > 4_096) {
      throw new Error("OIDC callback is invalid.");
    }
    currentUrl.search = input.callbackUrl.search;
    const tokens = await oidc.authorizationCodeGrant(
      configuration,
      currentUrl,
      {
        expectedNonce: input.nonce,
        expectedState: input.state,
        idTokenExpected: true,
        pkceCodeVerifier: input.codeVerifier,
      },
    );
    const claims = tokens.claims();
    if (!claims) throw new Error("OIDC ID token is missing.");
    this.validateClaims(claims);
    if (
      !validToken(tokens.access_token) ||
      !validToken(tokens.refresh_token) ||
      !validToken(tokens.id_token) ||
      tokens.token_type.toLowerCase() !== "bearer"
    ) {
      throw new Error("OIDC token response is invalid.");
    }
    return {
      subject: claims.sub,
      displayName: displayName(claims),
      accessToken: tokens.access_token,
      refreshToken: tokens.refresh_token,
      idToken: tokens.id_token,
      expiresAt: expiration(tokens, this.now()),
    };
  }

  async refresh(refreshToken: string): Promise<OidcRefreshSet> {
    if (!validToken(refreshToken)) throw new Error("Refresh token is invalid.");
    const configuration = await this.getConfiguration();
    const tokens = await oidc.refreshTokenGrant(configuration, refreshToken);
    if (
      !validToken(tokens.access_token) ||
      (tokens.refresh_token !== undefined &&
        !validToken(tokens.refresh_token)) ||
      (tokens.id_token !== undefined && !validToken(tokens.id_token)) ||
      tokens.token_type.toLowerCase() !== "bearer"
    ) {
      throw new Error("OIDC refresh response is invalid.");
    }
    const claims = tokens.claims();
    if (claims) this.validateClaims(claims);
    return {
      subject: claims?.sub,
      accessToken: tokens.access_token,
      refreshToken: tokens.refresh_token,
      idToken: tokens.id_token,
      expiresAt: expiration(tokens, this.now()),
    };
  }

  async revoke(
    token: string,
    hint: "access_token" | "refresh_token",
  ): Promise<void> {
    if (!validToken(token)) throw new Error("Token is invalid.");
    const configuration = await this.getConfiguration();
    await oidc.tokenRevocation(configuration, token, { token_type_hint: hint });
  }

  private validateClaims(
    claims: Record<string, unknown>,
  ): asserts claims is Record<string, unknown> & { sub: string } {
    const audience = claims.aud;
    const audienceMatches =
      audience === this.values.clientId ||
      (Array.isArray(audience) &&
        audience.length > 0 &&
        audience.every((value) => typeof value === "string") &&
        audience.includes(this.values.clientId));
    if (
      claims.iss !== this.values.issuer.href ||
      !audienceMatches ||
      typeof claims.sub !== "string" ||
      claims.sub.length === 0 ||
      claims.sub.length > 255 ||
      claims.sub !== claims.sub.trim() ||
      /[\u0000-\u001F\u007F]/u.test(claims.sub)
    ) {
      throw new Error("OIDC identity claims are invalid.");
    }
  }

  private getConfiguration(): Promise<oidc.Configuration> {
    this.configuration ??= this.discover().catch((error: unknown) => {
      this.configuration = undefined;
      throw error;
    });
    return this.configuration;
  }

  private async discover(): Promise<oidc.Configuration> {
    const boundedFetch: oidc.CustomFetch = async (rawUrl, options) => {
      const url = new URL(rawUrl);
      if (
        url.protocol !== "https:" ||
        url.origin !== this.values.issuer.origin ||
        url.username ||
        url.password ||
        url.hash ||
        options.redirect !== "manual"
      ) {
        throw new Error("OIDC network target is invalid.");
      }
      const response = await this.networkFetch(url, {
        ...options,
        redirect: "manual",
      } as unknown as RequestInit);
      if (response.status >= 300 && response.status < 400) {
        throw new Error("OIDC redirects are disabled.");
      }
      const body = await readBoundedResponse(response);
      return new Response(body, {
        status: response.status,
        statusText: response.statusText,
        headers: response.headers,
      });
    };

    const configuration = await oidc.discovery(
      this.values.issuer,
      this.values.clientId,
      {
        client_secret: this.values.clientSecret,
        redirect_uris: [this.values.redirectUri.href],
        response_types: ["code"],
        token_endpoint_auth_method: "client_secret_basic",
      },
      oidc.ClientSecretBasic(this.values.clientSecret),
      {
        [oidc.customFetch]: boundedFetch,
        timeout: this.values.timeout,
      },
    );
    this.validateMetadata(configuration.serverMetadata());
    return configuration;
  }

  private validateMetadata(metadata: Readonly<oidc.ServerMetadata>): void {
    if (metadata.issuer !== this.values.issuer.href) {
      throw new Error("OIDC issuer metadata is invalid.");
    }
    for (const name of [
      "authorization_endpoint",
      "jwks_uri",
      "revocation_endpoint",
      "token_endpoint",
    ] as const) {
      const value = metadata[name];
      if (!value) throw new Error("OIDC provider metadata is incomplete.");
      const url = new URL(value);
      if (
        url.protocol !== "https:" ||
        url.origin !== this.values.issuer.origin ||
        url.username ||
        url.password ||
        url.search ||
        url.hash
      ) {
        throw new Error("OIDC provider endpoint is invalid.");
      }
    }
  }
}
