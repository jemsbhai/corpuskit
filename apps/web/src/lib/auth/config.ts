import { AuthConfigurationError, type WebAuthConfig } from "./types";

const tokenPattern = /^[A-Za-z0-9._:/-]{1,512}$/u;
const redisPrefixPattern = /^[A-Za-z0-9:_-]{1,64}$/u;
const base64UrlPattern = /^[A-Za-z0-9_-]{43,684}$/u;
const returnPathPattern = /^\/[A-Za-z0-9._~\/-]*$/u;

function invalid(): never {
  throw new AuthConfigurationError();
}

function required(env: NodeJS.ProcessEnv, name: string): string {
  const value = env[name];
  if (!value || value !== value.trim()) invalid();
  return value;
}

function integer(
  env: NodeJS.ProcessEnv,
  name: string,
  fallback: number,
  minimum: number,
  maximum: number,
): number {
  const raw = env[name];
  if (raw === undefined || raw === "") return fallback;
  if (!/^[0-9]+$/u.test(raw)) invalid();
  const value = Number(raw);
  if (!Number.isSafeInteger(value) || value < minimum || value > maximum) {
    invalid();
  }
  return value;
}

function secret(env: NodeJS.ProcessEnv): Uint8Array {
  const encoded = required(env, "CORPUSKIT_WEB_STATE_SECRET");
  if (!base64UrlPattern.test(encoded)) invalid();
  const decoded = Buffer.from(encoded, "base64url");
  if (decoded.byteLength < 32 || decoded.byteLength > 512) invalid();
  if (decoded.toString("base64url") !== encoded) invalid();
  return new Uint8Array(decoded);
}

function encryptionKeys(env: NodeJS.ProcessEnv): readonly Uint8Array[] {
  let values: unknown;
  try {
    values = JSON.parse(required(env, "CORPUSKIT_WEB_SESSION_ENCRYPTION_KEYS"));
  } catch {
    invalid();
  }
  if (!Array.isArray(values) || values.length === 0 || values.length > 4) {
    invalid();
  }
  const keys = values.map((value) => {
    if (typeof value !== "string" || !base64UrlPattern.test(value)) invalid();
    const decoded = Buffer.from(value, "base64url");
    if (decoded.byteLength !== 32 || decoded.toString("base64url") !== value) {
      invalid();
    }
    return new Uint8Array(decoded);
  });
  const encoded = keys.map((value) => Buffer.from(value).toString("base64url"));
  if (new Set(encoded).size !== encoded.length) invalid();
  return keys;
}

function exactUrl(raw: string, protocol: "https:" | "redis"): URL {
  let url: URL;
  try {
    url = new URL(raw);
  } catch {
    invalid();
  }
  if (
    !url.hostname ||
    (protocol === "https:" && (url.username || url.password)) ||
    url.hash ||
    url.search
  ) {
    invalid();
  }
  if (protocol === "https:" && url.protocol !== "https:") invalid();
  if (
    protocol === "redis" &&
    url.protocol !== "redis:" &&
    url.protocol !== "rediss:"
  ) {
    invalid();
  }
  return url;
}

function returnPaths(env: NodeJS.ProcessEnv): ReadonlySet<string> {
  let values: unknown;
  try {
    values = JSON.parse(required(env, "CORPUSKIT_WEB_ALLOWED_RETURN_PATHS"));
  } catch {
    invalid();
  }
  if (!Array.isArray(values) || values.length === 0 || values.length > 32) {
    invalid();
  }
  const paths = new Set<string>();
  for (const value of values) {
    if (
      typeof value !== "string" ||
      value.length > 128 ||
      !returnPathPattern.test(value) ||
      value.includes("//") ||
      value.split("/").some((segment) => segment === "." || segment === "..") ||
      paths.has(value)
    ) {
      invalid();
    }
    paths.add(value);
  }
  if (!paths.has("/")) invalid();
  return paths;
}

function redisUrl(
  env: NodeJS.ProcessEnv,
  productionLike: boolean,
): URL | undefined {
  const raw = env.CORPUSKIT_WEB_SESSION_REDIS_URL;
  if (!raw) {
    if (productionLike) invalid();
    return undefined;
  }
  const url = exactUrl(raw, "redis");
  if (!/^\/(?:[0-9]|1[0-5])?$/u.test(url.pathname)) invalid();
  if (productionLike && (url.protocol !== "rediss:" || !url.password)) {
    invalid();
  }
  return url;
}

export function parseWebAuthConfig(
  env: NodeJS.ProcessEnv = process.env,
): WebAuthConfig {
  const environment = required(env, "CORPUSKIT_ENVIRONMENT");
  if (
    environment !== "development" &&
    environment !== "test" &&
    environment !== "staging" &&
    environment !== "production"
  ) {
    invalid();
  }
  const productionLike =
    environment === "production" || environment === "staging";
  const mode = required(env, "CORPUSKIT_WEB_AUTH_MODE");
  if (mode !== "demo" && mode !== "oidc" && mode !== "test") invalid();
  if (productionLike && mode !== "oidc") invalid();
  if (mode === "test" && environment !== "test") invalid();
  if (
    mode === "demo" &&
    environment !== "development" &&
    environment !== "test"
  ) {
    invalid();
  }

  const absolute = integer(
    env,
    "CORPUSKIT_WEB_SESSION_ABSOLUTE_SECONDS",
    28_800,
    300,
    86_400,
  );
  const idle = integer(
    env,
    "CORPUSKIT_WEB_SESSION_IDLE_SECONDS",
    1_800,
    60,
    absolute,
  );
  const refreshBefore = integer(
    env,
    "CORPUSKIT_WEB_TOKEN_REFRESH_BEFORE_SECONDS",
    60,
    5,
    300,
  );
  const transactionSeconds = integer(
    env,
    "CORPUSKIT_WEB_LOGIN_TRANSACTION_SECONDS",
    600,
    60,
    900,
  );
  const oidcTimeoutSeconds = integer(
    env,
    "CORPUSKIT_WEB_OIDC_TIMEOUT_SECONDS",
    5,
    1,
    10,
  );
  const redisCommandTimeoutMs = integer(
    env,
    "CORPUSKIT_WEB_REDIS_COMMAND_TIMEOUT_MS",
    2_000,
    100,
    10_000,
  );
  const redisPrefix = env.CORPUSKIT_WEB_REDIS_PREFIX ?? "corpuskit:web";
  if (!redisPrefixPattern.test(redisPrefix)) invalid();

  const common = {
    mode,
    environment,
    stateSecret: secret(env),
    sessionEncryptionKeys: encryptionKeys(env),
    allowedReturnPaths: returnPaths(env),
    sessionAbsoluteSeconds: absolute,
    sessionIdleSeconds: idle,
    refreshBeforeSeconds: refreshBefore,
    transactionSeconds,
    oidcTimeoutSeconds,
    redisUrl: redisUrl(env, productionLike),
    redisCommandTimeoutMs,
    redisPrefix,
  } as const;
  if (
    common.sessionEncryptionKeys.some((key) =>
      Buffer.from(key).equals(Buffer.from(common.stateSecret)),
    )
  ) {
    invalid();
  }

  if (mode !== "oidc") {
    return { ...common, scopes: "" };
  }

  const issuer = exactUrl(required(env, "CORPUSKIT_OIDC_ISSUER"), "https:");
  if (issuer.pathname.includes("/.well-known/") || issuer.port) invalid();
  const redirectUri = exactUrl(
    required(env, "CORPUSKIT_WEB_OIDC_REDIRECT_URI"),
    "https:",
  );
  if (redirectUri.pathname !== "/auth/callback" || redirectUri.port) invalid();
  const audience = required(env, "CORPUSKIT_OIDC_AUDIENCE");
  const clientId = required(env, "CORPUSKIT_WEB_OIDC_CLIENT_ID");
  const clientSecret = required(env, "CORPUSKIT_WEB_OIDC_CLIENT_SECRET");
  if (
    !tokenPattern.test(audience) ||
    !tokenPattern.test(clientId) ||
    clientSecret.length < 16 ||
    clientSecret.length > 1024
  ) {
    invalid();
  }
  const scopes =
    env.CORPUSKIT_WEB_OIDC_SCOPES ?? "openid profile offline_access";
  const scopeValues = scopes.split(" ");
  if (
    scopeValues.some((value) => !/^[A-Za-z0-9._:-]{1,64}$/u.test(value)) ||
    new Set(scopeValues).size !== scopeValues.length ||
    !scopeValues.includes("openid") ||
    !scopeValues.includes("offline_access")
  ) {
    invalid();
  }

  return {
    ...common,
    issuer,
    audience,
    clientId,
    clientSecret,
    redirectUri,
    scopes,
  };
}
