import { parseWebAuthConfig } from "./config";
import { OpenIdClientProvider } from "./oidc";
import { AuthService } from "./service";
import { MemorySessionStore, RedisSessionStore } from "./session-store";

export interface AuthRuntime {
  readonly service: AuthService;
}

let runtime: AuthRuntime | undefined;

export function getAuthRuntime(): AuthRuntime {
  if (runtime) return runtime;
  const config = parseWebAuthConfig();
  const store = config.redisUrl
    ? new RedisSessionStore(
        config.redisUrl,
        config.redisPrefix,
        config.redisCommandTimeoutMs,
        config.sessionEncryptionKeys,
      )
    : new MemorySessionStore();
  const provider =
    config.mode === "oidc" ? new OpenIdClientProvider(config) : undefined;
  runtime = { service: new AuthService(config, store, provider) };
  return runtime;
}

export function setAuthRuntimeForTests(value: AuthRuntime | undefined): void {
  if (process.env.NODE_ENV !== "test") {
    throw new Error("Auth runtime injection is test-only.");
  }
  runtime = value;
}
