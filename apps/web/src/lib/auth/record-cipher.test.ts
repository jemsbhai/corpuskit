import { describe, expect, it } from "vitest";

import { RecordCipher } from "./record-cipher";

const oldKey = new Uint8Array(Buffer.alloc(32, 1));
const newKey = new Uint8Array(Buffer.alloc(32, 2));

describe("session record encryption", () => {
  it("authenticates ciphertext and associated Redis keys without leaking tokens", () => {
    const cipher = new RecordCipher(
      [oldKey],
      () => new Uint8Array(Buffer.alloc(12, 3)),
    );
    const plaintext = JSON.stringify({ accessToken: "provider-secret-token" });
    const envelope = cipher.encrypt(plaintext, "prefix:session:id-one");
    expect(envelope).not.toContain("provider-secret-token");
    expect(cipher.decrypt(envelope, "prefix:session:id-one")).toBe(plaintext);
    expect(cipher.decrypt(envelope, "prefix:session:id-two")).toBeNull();
    expect(
      cipher.decrypt(`${envelope.slice(0, -1)}x`, "prefix:session:id-one"),
    ).toBeNull();
  });

  it("decrypts an old key during rotation and always writes with the active key", () => {
    const oldCipher = new RecordCipher([oldKey]);
    const rotatingCipher = new RecordCipher([newKey, oldKey]);
    const oldEnvelope = oldCipher.encrypt("record", "key");
    expect(rotatingCipher.decrypt(oldEnvelope, "key")).toBe("record");
    const rotatedEnvelope = rotatingCipher.encrypt("record", "key");
    expect(oldCipher.decrypt(rotatedEnvelope, "key")).toBeNull();
    expect(rotatingCipher.decrypt(rotatedEnvelope, "key")).toBe("record");
  });

  it("rejects malformed key rings, nonces, envelopes, and oversized records", () => {
    expect(() => new RecordCipher([])).toThrow(TypeError);
    expect(() => new RecordCipher([oldKey, oldKey])).toThrow(TypeError);
    expect(() => new RecordCipher([new Uint8Array(31)])).toThrow(TypeError);
    expect(() =>
      new RecordCipher([oldKey], () => new Uint8Array(11)).encrypt("x", "key"),
    ).toThrow(TypeError);
    const cipher = new RecordCipher([oldKey]);
    expect(() => cipher.encrypt("x".repeat(128 * 1024 + 1), "key")).toThrow(
      RangeError,
    );
    expect(cipher.decrypt("not-an-envelope", "key")).toBeNull();
    expect(cipher.decrypt("x".repeat(256 * 1024 + 1), "key")).toBeNull();

    const envelope = cipher.encrypt("record", "key");
    const parts = envelope.split(".");
    const malformed = [
      ["v2", ...parts.slice(1)].join("."),
      [parts[0], parts[1], "not+base64url", parts[3], parts[4]].join("."),
      [
        parts[0],
        parts[1],
        Buffer.alloc(11).toString("base64url"),
        parts[3],
        parts[4],
      ].join("."),
      [
        parts[0],
        parts[1],
        parts[2],
        parts[3],
        Buffer.alloc(15).toString("base64url"),
      ].join("."),
      [
        parts[0],
        parts[1],
        parts[2],
        Buffer.alloc(128 * 1024 + 1).toString("base64url"),
        parts[4],
      ].join("."),
    ];
    for (const value of malformed) {
      expect(cipher.decrypt(value, "key")).toBeNull();
    }
  });
});
