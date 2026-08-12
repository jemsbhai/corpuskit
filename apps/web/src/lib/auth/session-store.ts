import { randomBytes } from "node:crypto";

import { createClient } from "redis";

import { RecordCipher } from "./record-cipher";

import type {
  BrowserSession,
  LoginTransaction,
  SessionStore,
  WebAuthMode,
} from "./types";

interface Expiring<T> {
  readonly value: T;
  readonly expiresAt: number;
}

interface RedisClientLike {
  readonly isOpen: boolean;
  connect(): Promise<unknown>;
  get(key: string): Promise<string | null>;
  getDel(key: string): Promise<string | null>;
  set(
    key: string,
    value: string,
    options: { PX: number; NX?: boolean },
  ): Promise<string | null>;
  del(key: string): Promise<number>;
  eval(
    script: string,
    options: { keys: string[]; arguments: string[] },
  ): Promise<unknown>;
  destroy(): void;
  on(event: "error", listener: (error: Error) => void): unknown;
}

const opaquePattern = /^[A-Za-z0-9_-]{43,128}$/u;
const textPattern = /^[^\u0000-\u001F\u007F]{1,255}$/u;
const tokenPattern = /^[\u0021-\u007E]{1,65536}$/u;

function exactKeys(
  value: Record<string, unknown>,
  expected: readonly string[],
): boolean {
  return Object.keys(value).sort().join(",") === [...expected].sort().join(",");
}

function safeTime(value: unknown): value is number {
  return typeof value === "number" && Number.isSafeInteger(value) && value >= 0;
}

function optionalText(
  value: unknown,
  maximum: number,
): value is string | undefined {
  return (
    value === undefined ||
    (typeof value === "string" &&
      value.length <= maximum &&
      value === value.trim() &&
      textPattern.test(value))
  );
}

function optionalToken(value: unknown): value is string | undefined {
  return (
    value === undefined ||
    (typeof value === "string" && tokenPattern.test(value))
  );
}

function mode(value: unknown): value is WebAuthMode {
  return value === "demo" || value === "oidc" || value === "test";
}

export function decodeTransaction(raw: string): LoginTransaction | null {
  let value: unknown;
  try {
    value = JSON.parse(raw);
  } catch {
    return null;
  }
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const record = value as Record<string, unknown>;
  if (
    !exactKeys(record, [
      "codeVerifier",
      "createdAt",
      "expiresAt",
      "id",
      "nonce",
      "returnPath",
    ]) ||
    typeof record.id !== "string" ||
    !opaquePattern.test(record.id) ||
    typeof record.codeVerifier !== "string" ||
    !opaquePattern.test(record.codeVerifier) ||
    typeof record.nonce !== "string" ||
    !opaquePattern.test(record.nonce) ||
    typeof record.returnPath !== "string" ||
    record.returnPath.length > 128 ||
    !safeTime(record.createdAt) ||
    !safeTime(record.expiresAt) ||
    record.expiresAt <= record.createdAt
  ) {
    return null;
  }
  return record as unknown as LoginTransaction;
}

export function decodeSession(raw: string): BrowserSession | null {
  let value: unknown;
  try {
    value = JSON.parse(raw);
  } catch {
    return null;
  }
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const record = value as Record<string, unknown>;
  const required = [
    "absoluteExpiresAt",
    "createdAt",
    "csrfToken",
    "idleExpiresAt",
    "mode",
    "subject",
    "version",
  ];
  const optional = [
    "accessExpiresAt",
    "accessToken",
    "displayName",
    "idToken",
    "refreshToken",
  ];
  const keys = Object.keys(record);
  if (
    required.some((key) => !keys.includes(key)) ||
    keys.some((key) => !required.includes(key) && !optional.includes(key)) ||
    record.version !== 1 ||
    !mode(record.mode) ||
    typeof record.subject !== "string" ||
    record.subject !== record.subject.trim() ||
    !textPattern.test(record.subject) ||
    !optionalText(record.displayName, 160) ||
    !optionalToken(record.accessToken) ||
    !optionalToken(record.refreshToken) ||
    !optionalToken(record.idToken) ||
    (record.accessExpiresAt !== undefined &&
      !safeTime(record.accessExpiresAt)) ||
    typeof record.csrfToken !== "string" ||
    !opaquePattern.test(record.csrfToken) ||
    !safeTime(record.createdAt) ||
    !safeTime(record.absoluteExpiresAt) ||
    !safeTime(record.idleExpiresAt) ||
    record.absoluteExpiresAt <= record.createdAt ||
    record.idleExpiresAt <= record.createdAt ||
    record.idleExpiresAt > record.absoluteExpiresAt ||
    (record.mode === "oidc" &&
      (!record.accessToken || record.accessExpiresAt === undefined)) ||
    (record.mode !== "oidc" &&
      (record.accessToken !== undefined ||
        record.refreshToken !== undefined ||
        record.idToken !== undefined ||
        record.accessExpiresAt !== undefined))
  ) {
    return null;
  }
  return record as unknown as BrowserSession;
}

export class MemorySessionStore implements SessionStore {
  private readonly transactions = new Map<string, Expiring<LoginTransaction>>();
  private readonly sessions = new Map<string, Expiring<BrowserSession>>();
  private readonly locks = new Map<string, Promise<void>>();

  constructor(private readonly now: () => number = Date.now) {}

  async createTransaction(
    record: LoginTransaction,
    ttlMs: number,
  ): Promise<boolean> {
    this.purge(this.transactions, record.id);
    if (this.transactions.has(record.id)) return false;
    this.transactions.set(record.id, {
      value: structuredClone(record),
      expiresAt: this.expiration(ttlMs),
    });
    return true;
  }

  async consumeTransaction(id: string): Promise<LoginTransaction | null> {
    this.purge(this.transactions, id);
    const entry = this.transactions.get(id);
    this.transactions.delete(id);
    return entry ? structuredClone(entry.value) : null;
  }

  async getSession(id: string): Promise<BrowserSession | null> {
    this.purge(this.sessions, id);
    const entry = this.sessions.get(id);
    return entry ? structuredClone(entry.value) : null;
  }

  async saveSession(
    id: string,
    record: BrowserSession,
    ttlMs: number,
  ): Promise<void> {
    this.sessions.set(id, {
      value: structuredClone(record),
      expiresAt: this.expiration(ttlMs),
    });
  }

  async deleteSession(id: string): Promise<void> {
    this.sessions.delete(id);
  }

  async withSessionLock<T>(
    id: string,
    operation: () => Promise<T>,
  ): Promise<T> {
    const previous = this.locks.get(id) ?? Promise.resolve();
    let release!: () => void;
    const current = new Promise<void>((resolve) => {
      release = resolve;
    });
    const queued = previous.then(() => current);
    this.locks.set(id, queued);
    await previous;
    try {
      return await operation();
    } finally {
      release();
      if (this.locks.get(id) === queued) this.locks.delete(id);
    }
  }

  async close(): Promise<void> {}

  private expiration(ttlMs: number): number {
    if (!Number.isSafeInteger(ttlMs) || ttlMs <= 0)
      throw new TypeError("Invalid TTL.");
    return this.now() + ttlMs;
  }

  private purge<T>(values: Map<string, Expiring<T>>, key: string): void {
    const entry = values.get(key);
    if (entry && entry.expiresAt <= this.now()) values.delete(key);
  }
}

export class RedisSessionStore implements SessionStore {
  private readonly client: RedisClientLike;
  private readonly cipher: RecordCipher;
  private connecting?: Promise<unknown>;

  constructor(
    url: URL,
    private readonly prefix: string,
    private readonly commandTimeoutMs: number,
    encryptionKeys: readonly Uint8Array[],
    client?: RedisClientLike,
  ) {
    this.cipher = new RecordCipher(encryptionKeys);
    this.client =
      client ??
      (createClient({
        url: url.href,
        commandsQueueMaxLength: 128,
        disableOfflineQueue: true,
        socket: {
          connectTimeout: commandTimeoutMs,
          reconnectStrategy: false,
        },
      }) as RedisClientLike);
    this.client.on("error", () => undefined);
  }

  async createTransaction(
    record: LoginTransaction,
    ttlMs: number,
  ): Promise<boolean> {
    await this.ready();
    const key = this.key("login", record.id);
    const result = await this.bounded(
      this.client.set(key, this.cipher.encrypt(JSON.stringify(record), key), {
        PX: this.ttl(ttlMs),
        NX: true,
      }),
    );
    return result === "OK";
  }

  async consumeTransaction(id: string): Promise<LoginTransaction | null> {
    await this.ready();
    const key = this.key("login", id);
    const raw = await this.bounded(this.client.getDel(key));
    const plaintext = raw ? this.cipher.decrypt(raw, key) : null;
    return plaintext ? decodeTransaction(plaintext) : null;
  }

  async getSession(id: string): Promise<BrowserSession | null> {
    await this.ready();
    const key = this.key("session", id);
    const raw = await this.bounded(this.client.get(key));
    const plaintext = raw ? this.cipher.decrypt(raw, key) : null;
    return plaintext ? decodeSession(plaintext) : null;
  }

  async saveSession(
    id: string,
    record: BrowserSession,
    ttlMs: number,
  ): Promise<void> {
    await this.ready();
    const key = this.key("session", id);
    const result = await this.bounded(
      this.client.set(key, this.cipher.encrypt(JSON.stringify(record), key), {
        PX: this.ttl(ttlMs),
      }),
    );
    if (result !== "OK") throw new Error("Session write failed.");
  }

  async deleteSession(id: string): Promise<void> {
    await this.ready();
    await this.bounded(this.client.del(this.key("session", id)));
  }

  async withSessionLock<T>(
    id: string,
    operation: () => Promise<T>,
  ): Promise<T> {
    await this.ready();
    const key = this.key("lock", id);
    const token = randomBytes(32).toString("base64url");
    const deadline = Date.now() + this.commandTimeoutMs;
    let acquired = false;
    while (!acquired && Date.now() < deadline) {
      acquired =
        (await this.bounded(
          this.client.set(key, token, { PX: 60_000, NX: true }),
        )) === "OK";
      if (!acquired) {
        await new Promise((resolve) => setTimeout(resolve, 20));
      }
    }
    if (!acquired) throw new Error("Session lock is unavailable.");
    try {
      return await operation();
    } finally {
      await this.bounded(
        this.client.eval(
          "if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('del', KEYS[1]) else return 0 end",
          { keys: [key], arguments: [token] },
        ),
      );
    }
  }

  async close(): Promise<void> {
    if (this.client.isOpen) this.client.destroy();
  }

  private key(kind: "lock" | "login" | "session", id: string): string {
    if (!opaquePattern.test(id)) throw new TypeError("Invalid store key.");
    return `${this.prefix}:${kind}:${id}`;
  }

  private ttl(ttlMs: number): number {
    if (!Number.isSafeInteger(ttlMs) || ttlMs <= 0 || ttlMs > 86_400_000) {
      throw new TypeError("Invalid TTL.");
    }
    return ttlMs;
  }

  private async ready(): Promise<void> {
    if (this.client.isOpen) return;
    this.connecting ??= this.bounded(this.client.connect());
    try {
      await this.connecting;
    } finally {
      this.connecting = undefined;
    }
  }

  private async bounded<T>(operation: Promise<T>): Promise<T> {
    let timer: ReturnType<typeof setTimeout> | undefined;
    const timeout = new Promise<never>((_resolve, reject) => {
      timer = setTimeout(
        () => reject(new Error("Session operation timed out.")),
        this.commandTimeoutMs,
      );
    });
    try {
      return await Promise.race([operation, timeout]);
    } finally {
      clearTimeout(timer);
    }
  }
}
