import { describe, expect, it, vi } from "vitest";

import {
  decodeSession,
  decodeTransaction,
  MemorySessionStore,
  RedisSessionStore,
} from "./session-store";
import type { BrowserSession, LoginTransaction } from "./types";

const id = Buffer.alloc(32, 3).toString("base64url");
const csrf = Buffer.alloc(32, 4).toString("base64url");
const transaction: LoginTransaction = {
  id,
  codeVerifier: Buffer.alloc(32, 5).toString("base64url"),
  nonce: Buffer.alloc(32, 6).toString("base64url"),
  returnPath: "/",
  createdAt: 1_000,
  expiresAt: 2_000,
};
const session: BrowserSession = {
  version: 1,
  mode: "oidc",
  subject: "subject-1",
  displayName: "A user",
  accessToken: "access-token",
  refreshToken: "refresh-token",
  idToken: "id-token",
  accessExpiresAt: 2_000,
  csrfToken: csrf,
  createdAt: 1_000,
  absoluteExpiresAt: 4_000,
  idleExpiresAt: 3_000,
};

class FakeRedis {
  isOpen = false;
  readonly values = new Map<string, string>();
  readonly connect = vi.fn(async () => {
    this.isOpen = true;
  });
  readonly on = vi.fn(() => this);
  readonly destroy = vi.fn(() => {
    this.isOpen = false;
  });

  async get(key: string) {
    return this.values.get(key) ?? null;
  }

  async getDel(key: string) {
    const value = this.values.get(key) ?? null;
    this.values.delete(key);
    return value;
  }

  async set(key: string, value: string, options: { PX: number; NX?: boolean }) {
    if (options.NX && this.values.has(key)) return null;
    this.values.set(key, value);
    return "OK";
  }

  async del(key: string) {
    return this.values.delete(key) ? 1 : 0;
  }

  async eval(
    _script: string,
    options: { keys: string[]; arguments: string[] },
  ) {
    const key = options.keys[0] ?? "";
    if (this.values.get(key) !== options.arguments[0]) return 0;
    return this.values.delete(key) ? 1 : 0;
  }
}

const encryptionKeys = [new Uint8Array(Buffer.alloc(32, 77))];

describe("session record codecs", () => {
  it("accepts strict records and rejects corrupted or expanded records", () => {
    expect(decodeTransaction(JSON.stringify(transaction))).toEqual(transaction);
    expect(decodeSession(JSON.stringify(session))).toEqual(session);
    expect(decodeTransaction("not-json")).toBeNull();
    expect(decodeTransaction("[]")).toBeNull();
    expect(
      decodeTransaction(JSON.stringify({ ...transaction, unexpected: true })),
    ).toBeNull();
    expect(
      decodeTransaction(JSON.stringify({ ...transaction, expiresAt: 500 })),
    ).toBeNull();
    expect(decodeSession("not-json")).toBeNull();
    expect(decodeSession("[]")).toBeNull();
    expect(
      decodeSession(JSON.stringify({ ...session, version: 2 })),
    ).toBeNull();
    expect(
      decodeSession(JSON.stringify({ ...session, idleExpiresAt: 5_000 })),
    ).toBeNull();
    expect(
      decodeSession(JSON.stringify({ ...session, accessToken: undefined })),
    ).toBeNull();
    expect(
      decodeSession(
        JSON.stringify({
          ...session,
          mode: "demo",
          accessExpiresAt: undefined,
          accessToken: undefined,
          refreshToken: undefined,
          idToken: undefined,
        }),
      ),
    ).not.toBeNull();
    expect(
      decodeSession(
        JSON.stringify({
          ...session,
          mode: "test",
          accessExpiresAt: undefined,
          accessToken: undefined,
          refreshToken: undefined,
          idToken: undefined,
        }),
      ),
    ).not.toBeNull();
  });
});

describe("MemorySessionStore", () => {
  it("atomically consumes login transactions and expires records", async () => {
    let now = 1_000;
    const store = new MemorySessionStore(() => now);
    await expect(store.createTransaction(transaction, 100)).resolves.toBe(true);
    await expect(store.createTransaction(transaction, 100)).resolves.toBe(
      false,
    );
    await expect(store.consumeTransaction(id)).resolves.toEqual(transaction);
    await expect(store.consumeTransaction(id)).resolves.toBeNull();

    await store.saveSession(id, session, 100);
    const loaded = await store.getSession(id);
    expect(loaded).toEqual(session);
    expect(loaded).not.toBe(session);
    now = 1_101;
    await expect(store.getSession(id)).resolves.toBeNull();
    await expect(store.createTransaction(transaction, 0)).rejects.toThrow(
      TypeError,
    );
    await store.close();
  });

  it("deletes sessions", async () => {
    const store = new MemorySessionStore(() => 1_000);
    await store.saveSession(id, session, 100);
    await store.deleteSession(id);
    await expect(store.getSession(id)).resolves.toBeNull();
  });

  it("serializes concurrent in-memory operations for the same session", async () => {
    const store = new MemorySessionStore();
    const events: string[] = [];
    let release!: () => void;
    const first = store.withSessionLock(id, async () => {
      events.push("first-start");
      await new Promise<void>((resolve) => {
        release = resolve;
      });
      events.push("first-end");
    });
    await vi.waitFor(() => expect(release).toBeTypeOf("function"));
    const second = store.withSessionLock(id, async () => {
      events.push("second");
    });
    await Promise.resolve();
    expect(events).toEqual(["first-start"]);
    release();
    await Promise.all([first, second]);
    expect(events).toEqual(["first-start", "first-end", "second"]);
  });
});

describe("RedisSessionStore", () => {
  it("uses namespaced, one-time, TTL-bounded Redis records", async () => {
    const client = new FakeRedis();
    const store = new RedisSessionStore(
      new URL("rediss://u:p@cache.example:6379/0"),
      "test:web",
      100,
      encryptionKeys,
      client,
    );
    await expect(store.createTransaction(transaction, 100)).resolves.toBe(true);
    await expect(store.createTransaction(transaction, 100)).resolves.toBe(
      false,
    );
    expect([...client.values.keys()][0]).toBe(`test:web:login:${id}`);
    await expect(store.consumeTransaction(id)).resolves.toEqual(transaction);
    await expect(store.consumeTransaction(id)).resolves.toBeNull();
    await store.saveSession(id, session, 100);
    expect([...client.values.values()].join("\n")).not.toContain(
      "access-token",
    );
    expect([...client.values.values()].join("\n")).not.toContain(
      "refresh-token",
    );
    await expect(store.getSession(id)).resolves.toEqual(session);
    await store.deleteSession(id);
    await expect(store.getSession(id)).resolves.toBeNull();
    expect(client.connect).toHaveBeenCalledTimes(1);
    await store.close();
    expect(client.destroy).toHaveBeenCalledOnce();
  });

  it("fails closed on Redis outage and command timeout without logging details", async () => {
    const consoleSpy = vi
      .spyOn(console, "error")
      .mockImplementation(() => undefined);
    const failed = new FakeRedis();
    failed.connect.mockRejectedValueOnce(new Error("secret redis endpoint"));
    const unavailable = new RedisSessionStore(
      new URL("rediss://u:p@cache.example:6379/0"),
      "test:web",
      20,
      encryptionKeys,
      failed,
    );
    await expect(unavailable.getSession(id)).rejects.toThrow();

    const hung = new FakeRedis();
    hung.isOpen = true;
    hung.get = vi.fn(() => new Promise<string | null>(() => undefined));
    const timedOut = new RedisSessionStore(
      new URL("rediss://u:p@cache.example:6379/0"),
      "test:web",
      5,
      encryptionKeys,
      hung,
    );
    await expect(timedOut.getSession(id)).rejects.toThrow(
      "Session operation timed out.",
    );
    expect(consoleSpy).not.toHaveBeenCalled();
    consoleSpy.mockRestore();
  });

  it("rejects invalid keys and TTLs before Redis mutation", async () => {
    const client = new FakeRedis();
    client.isOpen = true;
    const store = new RedisSessionStore(
      new URL("rediss://u:p@cache.example:6379/0"),
      "test:web",
      100,
      encryptionKeys,
      client,
    );
    await expect(store.getSession("short")).rejects.toThrow(TypeError);
    await expect(store.saveSession(id, session, 0)).rejects.toThrow(TypeError);
    await expect(store.saveSession(id, session, 86_400_001)).rejects.toThrow(
      TypeError,
    );
  });

  it("constructs and closes the production Redis client without an offline queue", async () => {
    const store = new RedisSessionStore(
      new URL("rediss://u:p@cache.example:6379/0"),
      "test:web",
      100,
      encryptionKeys,
    );
    await store.close();
  });

  it("fails closed on rejected writes and unavailable distributed locks", async () => {
    const writeClient = new FakeRedis();
    writeClient.isOpen = true;
    vi.spyOn(writeClient, "set").mockResolvedValueOnce(null);
    const writeStore = new RedisSessionStore(
      new URL("rediss://u:p@cache.example:6379/0"),
      "test:web",
      100,
      encryptionKeys,
      writeClient,
    );
    await expect(writeStore.saveSession(id, session, 100)).rejects.toThrow(
      "Session write failed.",
    );

    const lockClient = new FakeRedis();
    lockClient.isOpen = true;
    vi.spyOn(lockClient, "set").mockResolvedValue(null);
    const lockStore = new RedisSessionStore(
      new URL("rediss://u:p@cache.example:6379/0"),
      "test:web",
      1,
      encryptionKeys,
      lockClient,
    );
    await expect(
      lockStore.withSessionLock(id, async () => undefined),
    ).rejects.toThrow("Session lock is unavailable.");
  });

  it("decrypts old records during key rotation and rewrites with the active key", async () => {
    const client = new FakeRedis();
    client.isOpen = true;
    const oldStore = new RedisSessionStore(
      new URL("rediss://u:p@cache.example:6379/0"),
      "test:web",
      100,
      [new Uint8Array(Buffer.alloc(32, 70))],
      client,
    );
    await oldStore.saveSession(id, session, 100);
    const rotatingStore = new RedisSessionStore(
      new URL("rediss://u:p@cache.example:6379/0"),
      "test:web",
      100,
      [
        new Uint8Array(Buffer.alloc(32, 71)),
        new Uint8Array(Buffer.alloc(32, 70)),
      ],
      client,
    );
    await expect(rotatingStore.getSession(id)).resolves.toEqual(session);
    await rotatingStore.saveSession(id, session, 100);
    await expect(oldStore.getSession(id)).resolves.toBeNull();
    await expect(rotatingStore.getSession(id)).resolves.toEqual(session);
  });

  it("serializes the same session across Redis-backed application instances", async () => {
    const client = new FakeRedis();
    client.isOpen = true;
    const first = new RedisSessionStore(
      new URL("rediss://u:p@cache.example:6379/0"),
      "test:web",
      200,
      encryptionKeys,
      client,
    );
    const second = new RedisSessionStore(
      new URL("rediss://u:p@cache.example:6379/0"),
      "test:web",
      200,
      encryptionKeys,
      client,
    );
    const events: string[] = [];
    let release: (() => void) | undefined;
    const firstOperation = first.withSessionLock(id, async () => {
      events.push("first-start");
      await new Promise<void>((resolve) => {
        release = resolve;
      });
      events.push("first-end");
    });
    await vi.waitFor(() => expect(release).toBeTypeOf("function"));
    const secondOperation = second.withSessionLock(id, async () => {
      events.push("second");
    });
    await new Promise((resolve) => setTimeout(resolve, 30));
    expect(events).toEqual(["first-start"]);
    release?.();
    await Promise.all([firstOperation, secondOperation]);
    expect(events).toEqual(["first-start", "first-end", "second"]);
  });
});
