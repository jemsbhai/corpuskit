export type WebAuthMode = "demo" | "oidc" | "test";

export type WebEnvironment = "development" | "production" | "staging" | "test";

export interface WebAuthConfig {
  readonly mode: WebAuthMode;
  readonly environment: WebEnvironment;
  readonly issuer?: URL;
  readonly audience?: string;
  readonly clientId?: string;
  readonly clientSecret?: string;
  readonly redirectUri?: URL;
  readonly stateSecret: Uint8Array;
  readonly sessionEncryptionKeys: readonly Uint8Array[];
  readonly scopes: string;
  readonly allowedReturnPaths: ReadonlySet<string>;
  readonly sessionAbsoluteSeconds: number;
  readonly sessionIdleSeconds: number;
  readonly refreshBeforeSeconds: number;
  readonly transactionSeconds: number;
  readonly oidcTimeoutSeconds: number;
  readonly redisUrl?: URL;
  readonly redisCommandTimeoutMs: number;
  readonly redisPrefix: string;
}

export interface LoginTransaction {
  readonly id: string;
  readonly codeVerifier: string;
  readonly nonce: string;
  readonly returnPath: string;
  readonly createdAt: number;
  readonly expiresAt: number;
}

export interface BrowserSession {
  readonly version: 1;
  readonly mode: WebAuthMode;
  readonly subject: string;
  readonly displayName?: string;
  readonly accessToken?: string;
  readonly refreshToken?: string;
  readonly idToken?: string;
  readonly accessExpiresAt?: number;
  readonly csrfToken: string;
  readonly createdAt: number;
  readonly absoluteExpiresAt: number;
  readonly idleExpiresAt: number;
}

export interface OidcTokenSet {
  readonly subject: string;
  readonly displayName?: string;
  readonly accessToken: string;
  readonly refreshToken?: string;
  readonly idToken?: string;
  readonly expiresAt: number;
}

export interface OidcRefreshSet {
  readonly subject?: string;
  readonly accessToken: string;
  readonly refreshToken?: string;
  readonly idToken?: string;
  readonly expiresAt: number;
}

export interface OidcProvider {
  authorizationUrl(input: {
    readonly state: string;
    readonly nonce: string;
    readonly codeVerifier: string;
  }): Promise<URL>;
  exchange(input: {
    readonly callbackUrl: URL;
    readonly state: string;
    readonly nonce: string;
    readonly codeVerifier: string;
  }): Promise<OidcTokenSet>;
  refresh(refreshToken: string): Promise<OidcRefreshSet>;
  revoke(token: string, hint: "access_token" | "refresh_token"): Promise<void>;
}

export interface SessionStore {
  createTransaction(record: LoginTransaction, ttlMs: number): Promise<boolean>;
  consumeTransaction(id: string): Promise<LoginTransaction | null>;
  getSession(id: string): Promise<BrowserSession | null>;
  saveSession(id: string, record: BrowserSession, ttlMs: number): Promise<void>;
  deleteSession(id: string): Promise<void>;
  withSessionLock<T>(id: string, operation: () => Promise<T>): Promise<T>;
  close(): Promise<void>;
}

export type AuthErrorCode =
  | "authentication_required"
  | "authentication_unavailable"
  | "csrf_validation_failed"
  | "invalid_authentication_callback"
  | "invalid_return_path";

export class AuthError extends Error {
  constructor(
    readonly code: AuthErrorCode,
    readonly status: number,
  ) {
    super(code);
    this.name = "AuthError";
  }
}

export class AuthConfigurationError extends Error {
  constructor() {
    super("Web authentication is not configured safely.");
    this.name = "AuthConfigurationError";
  }
}
