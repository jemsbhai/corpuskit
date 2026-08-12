import type {
  BrowserSession,
  LoginTransaction,
  OidcProvider,
  SessionStore,
  WebAuthConfig,
} from "./types";
import { AuthError } from "./types";
import {
  constantTimeEqual,
  randomOpaque,
  type RandomSource,
  secureRandom,
  StateSigner,
} from "./security";

export interface LoginStart {
  readonly authorizationUrl: URL;
  readonly correlationId: string;
  readonly maximumAge: number;
}

export interface LoginCompletion {
  readonly sessionId: string;
  readonly returnPath: string;
  readonly maximumAge: number;
}

export interface ResolvedSession {
  readonly id: string;
  readonly value: BrowserSession;
  readonly maximumAge: number;
}

export interface PublicSession {
  readonly authenticated: true;
  readonly csrfToken: string;
  readonly subject: string;
  readonly displayName?: string;
  readonly expiresAt: string;
}

export class AuthService {
  private readonly signer: StateSigner;

  constructor(
    readonly config: WebAuthConfig,
    private readonly store: SessionStore,
    private readonly provider?: OidcProvider,
    private readonly now: () => number = Date.now,
    private readonly random: RandomSource = secureRandom,
  ) {
    this.signer = new StateSigner(
      config.stateSecret,
      config.transactionSeconds,
    );
    if ((config.mode === "oidc") !== Boolean(provider)) {
      throw new TypeError(
        "OIDC provider configuration does not match auth mode.",
      );
    }
  }

  async beginLogin(returnPath: string): Promise<LoginStart> {
    if (!this.provider || this.config.mode !== "oidc") {
      throw new AuthError("authentication_unavailable", 503);
    }
    if (!this.config.allowedReturnPaths.has(returnPath)) {
      throw new AuthError("invalid_return_path", 400);
    }
    const createdAt = this.now();
    let transaction: LoginTransaction | undefined;
    for (let attempt = 0; attempt < 3 && !transaction; attempt += 1) {
      const candidate: LoginTransaction = {
        id: randomOpaque(this.random),
        codeVerifier: randomOpaque(this.random),
        nonce: randomOpaque(this.random),
        returnPath,
        createdAt,
        expiresAt: createdAt + this.config.transactionSeconds * 1_000,
      };
      const created = await this.storeOperation(
        this.store.createTransaction(
          candidate,
          this.config.transactionSeconds * 1_000,
        ),
      );
      if (created) transaction = candidate;
    }
    if (!transaction) throw new AuthError("authentication_unavailable", 503);
    const state = this.signer.sign(
      transaction.id,
      Math.floor(createdAt / 1_000),
    );
    let authorizationUrl: URL;
    try {
      authorizationUrl = await this.provider.authorizationUrl({
        state,
        nonce: transaction.nonce,
        codeVerifier: transaction.codeVerifier,
      });
    } catch {
      throw new AuthError("authentication_unavailable", 503);
    }
    return {
      authorizationUrl,
      correlationId: transaction.id,
      maximumAge: this.config.transactionSeconds,
    };
  }

  async completeLogin(
    callbackUrl: URL,
    correlationId: string | null,
    previousSessionId: string | null,
  ): Promise<LoginCompletion> {
    if (!this.provider || this.config.mode !== "oidc") {
      throw new AuthError("authentication_unavailable", 503);
    }
    const states = callbackUrl.searchParams.getAll("state");
    if (states.length !== 1 || !correlationId) this.invalidCallback();
    const state = states[0]!;
    const signed = this.signer.verify(state, Math.floor(this.now() / 1_000));
    if (!constantTimeEqual(signed.id, correlationId)) this.invalidCallback();
    const transaction = await this.storeOperation(
      this.store.consumeTransaction(signed.id),
    );
    const currentTime = this.now();
    if (
      !transaction ||
      transaction.expiresAt <= currentTime ||
      transaction.createdAt > currentTime + 30_000 ||
      transaction.id !== signed.id ||
      !this.config.allowedReturnPaths.has(transaction.returnPath)
    ) {
      this.invalidCallback();
    }

    let tokens;
    try {
      tokens = await this.provider.exchange({
        callbackUrl,
        state,
        nonce: transaction.nonce,
        codeVerifier: transaction.codeVerifier,
      });
    } catch {
      this.invalidCallback();
    }
    const absoluteExpiresAt =
      currentTime + this.config.sessionAbsoluteSeconds * 1_000;
    const session: BrowserSession = {
      version: 1,
      mode: "oidc",
      subject: tokens.subject,
      ...(tokens.displayName ? { displayName: tokens.displayName } : {}),
      accessToken: tokens.accessToken,
      refreshToken: tokens.refreshToken,
      idToken: tokens.idToken,
      accessExpiresAt: tokens.expiresAt,
      csrfToken: randomOpaque(this.random),
      createdAt: currentTime,
      absoluteExpiresAt,
      idleExpiresAt: Math.min(
        absoluteExpiresAt,
        currentTime + this.config.sessionIdleSeconds * 1_000,
      ),
    };
    let sessionId = randomOpaque(this.random);
    for (
      let attempt = 0;
      previousSessionId &&
      constantTimeEqual(sessionId, previousSessionId) &&
      attempt < 3;
      attempt += 1
    ) {
      sessionId = randomOpaque(this.random);
    }
    if (previousSessionId && constantTimeEqual(sessionId, previousSessionId)) {
      throw new AuthError("authentication_unavailable", 503);
    }
    if (previousSessionId) {
      await this.storeOperation(
        this.store.withSessionLock(previousSessionId, () =>
          this.store.deleteSession(previousSessionId),
        ),
      );
    }
    await this.save(sessionId, session);
    return {
      sessionId,
      returnPath: transaction.returnPath,
      maximumAge: this.maximumAge(session),
    };
  }

  async resolveSession(id: string | null): Promise<ResolvedSession | null> {
    if (!id) return null;
    return this.storeOperation(
      this.store.withSessionLock(id, async () => {
        let session = await this.store.getSession(id);
        if (!session || session.mode !== this.config.mode) return null;
        const currentTime = this.now();
        if (
          session.absoluteExpiresAt <= currentTime ||
          session.idleExpiresAt <= currentTime
        ) {
          await this.store.deleteSession(id);
          return null;
        }
        if (
          session.mode === "oidc" &&
          (session.accessExpiresAt ?? 0) <=
            currentTime + this.config.refreshBeforeSeconds * 1_000
        ) {
          session = await this.refreshLocked(id, session);
        }
        const idleExpiresAt = Math.min(
          session.absoluteExpiresAt,
          currentTime + this.config.sessionIdleSeconds * 1_000,
        );
        session = { ...session, idleExpiresAt };
        await this.store.saveSession(
          id,
          session,
          this.maximumAge(session) * 1_000,
        );
        return { id, value: session, maximumAge: this.maximumAge(session) };
      }),
    );
  }

  async bootstrapLocalSession(): Promise<ResolvedSession> {
    if (this.config.mode === "oidc") {
      throw new AuthError("authentication_required", 401);
    }
    const currentTime = this.now();
    const absoluteExpiresAt =
      currentTime + this.config.sessionAbsoluteSeconds * 1_000;
    const session: BrowserSession = {
      version: 1,
      mode: this.config.mode,
      subject:
        this.config.mode === "demo" ? "demo-user" : "deterministic-test-user",
      displayName: this.config.mode === "demo" ? "Demo user" : "Test user",
      csrfToken: randomOpaque(this.random),
      createdAt: currentTime,
      absoluteExpiresAt,
      idleExpiresAt: Math.min(
        absoluteExpiresAt,
        currentTime + this.config.sessionIdleSeconds * 1_000,
      ),
    };
    const id = randomOpaque(this.random);
    await this.save(id, session);
    return { id, value: session, maximumAge: this.maximumAge(session) };
  }

  csrfMatches(session: BrowserSession, candidate: string | null): boolean {
    return (
      candidate !== null && constantTimeEqual(session.csrfToken, candidate)
    );
  }

  publicSession(session: BrowserSession): PublicSession {
    return {
      authenticated: true,
      csrfToken: session.csrfToken,
      subject: session.subject,
      ...(session.displayName ? { displayName: session.displayName } : {}),
      expiresAt: new Date(
        Math.min(session.absoluteExpiresAt, session.idleExpiresAt),
      ).toISOString(),
    };
  }

  async logout(id: string | null, csrfToken: string | null): Promise<void> {
    if (!id) throw new AuthError("authentication_required", 401);
    const session = await this.storeOperation(
      this.store.withSessionLock(id, async () => {
        const current = await this.store.getSession(id);
        if (!current) throw new AuthError("authentication_required", 401);
        if (!this.csrfMatches(current, csrfToken)) {
          throw new AuthError("csrf_validation_failed", 403);
        }
        await this.store.deleteSession(id);
        return current;
      }),
    );
    if (!this.provider || session.mode !== "oidc") return;
    const revocations: Promise<void>[] = [];
    if (session.refreshToken) {
      revocations.push(
        this.provider.revoke(session.refreshToken, "refresh_token"),
      );
    }
    if (session.accessToken) {
      revocations.push(
        this.provider.revoke(session.accessToken, "access_token"),
      );
    }
    await Promise.allSettled(revocations);
  }

  async close(): Promise<void> {
    await this.store.close();
  }

  private async refreshLocked(
    id: string,
    session: BrowserSession,
  ): Promise<BrowserSession> {
    if (!this.provider || !session.refreshToken) {
      await this.store.deleteSession(id);
      throw new AuthError("authentication_required", 401);
    }
    try {
      const refreshed = await this.provider.refresh(session.refreshToken);
      if (refreshed.subject && refreshed.subject !== session.subject) {
        throw new Error("OIDC subject changed during refresh.");
      }
      const next: BrowserSession = {
        ...session,
        accessToken: refreshed.accessToken,
        accessExpiresAt: refreshed.expiresAt,
        refreshToken: refreshed.refreshToken ?? session.refreshToken,
        idToken: refreshed.idToken ?? session.idToken,
      };
      await this.store.saveSession(id, next, this.maximumAge(next) * 1_000);
      return next;
    } catch (error) {
      try {
        await this.store.deleteSession(id);
      } catch {
        // The request still fails closed; the stale record remains bounded by its TTL.
      }
      if (error instanceof AuthError) throw error;
      throw new AuthError("authentication_unavailable", 503);
    }
  }

  private async save(id: string, session: BrowserSession): Promise<void> {
    await this.storeOperation(
      this.store.saveSession(id, session, this.maximumAge(session) * 1_000),
    );
  }

  private maximumAge(session: BrowserSession): number {
    return Math.max(
      1,
      Math.ceil(
        (Math.min(session.absoluteExpiresAt, session.idleExpiresAt) -
          this.now()) /
          1_000,
      ),
    );
  }

  private async storeOperation<T>(operation: Promise<T>): Promise<T> {
    try {
      return await operation;
    } catch (error) {
      if (error instanceof AuthError) throw error;
      throw new AuthError("authentication_unavailable", 503);
    }
  }

  private invalidCallback(): never {
    throw new AuthError("invalid_authentication_callback", 400);
  }
}
