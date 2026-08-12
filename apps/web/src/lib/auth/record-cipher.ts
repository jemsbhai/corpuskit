import {
  createCipheriv,
  createDecipheriv,
  createHash,
  randomBytes,
} from "node:crypto";

const maximumPlaintextBytes = 128 * 1024;
const maximumEnvelopeCharacters = 256 * 1024;
const envelopePartPattern = /^[A-Za-z0-9_-]+$/u;

interface KeyRecord {
  readonly id: string;
  readonly value: Uint8Array;
}

export class RecordCipher {
  private readonly keys: readonly KeyRecord[];

  constructor(
    keys: readonly Uint8Array[],
    private readonly nonce: () => Uint8Array = () =>
      new Uint8Array(randomBytes(12)),
  ) {
    if (keys.length === 0 || keys.length > 4)
      throw new TypeError("Invalid key ring.");
    this.keys = keys.map((value) => {
      if (value.byteLength !== 32)
        throw new TypeError("Invalid encryption key.");
      return {
        id: createHash("sha256").update(value).digest("base64url").slice(0, 16),
        value: new Uint8Array(value),
      };
    });
    if (new Set(this.keys.map(({ id }) => id)).size !== this.keys.length) {
      throw new TypeError("Duplicate encryption key.");
    }
  }

  encrypt(plaintext: string, associatedData: string): string {
    const source = Buffer.from(plaintext, "utf8");
    if (source.byteLength > maximumPlaintextBytes) {
      throw new RangeError("Session record is too large.");
    }
    // The constructor establishes the non-empty key-ring invariant.
    const active = this.keys[0]!;
    const iv = this.nonce();
    if (iv.byteLength !== 12) throw new TypeError("Invalid encryption nonce.");
    const cipher = createCipheriv("aes-256-gcm", active.value, iv);
    cipher.setAAD(Buffer.from(associatedData, "utf8"));
    const ciphertext = Buffer.concat([cipher.update(source), cipher.final()]);
    const tag = cipher.getAuthTag();
    return [
      "v1",
      active.id,
      Buffer.from(iv).toString("base64url"),
      ciphertext.toString("base64url"),
      tag.toString("base64url"),
    ].join(".");
  }

  decrypt(envelope: string, associatedData: string): string | null {
    if (envelope.length > maximumEnvelopeCharacters) return null;
    const parts = envelope.split(".");
    if (
      parts.length !== 5 ||
      parts[0] !== "v1" ||
      parts.slice(1).some((part) => !envelopePartPattern.test(part))
    ) {
      return null;
    }
    const key = this.keys.find(({ id }) => id === parts[1]);
    if (!key) return null;
    try {
      const iv = Buffer.from(parts[2]!, "base64url");
      const ciphertext = Buffer.from(parts[3]!, "base64url");
      const tag = Buffer.from(parts[4]!, "base64url");
      if (
        iv.byteLength !== 12 ||
        tag.byteLength !== 16 ||
        ciphertext.byteLength > maximumPlaintextBytes
      ) {
        return null;
      }
      const decipher = createDecipheriv("aes-256-gcm", key.value, iv);
      decipher.setAAD(Buffer.from(associatedData, "utf8"));
      decipher.setAuthTag(tag);
      return Buffer.concat([
        decipher.update(ciphertext),
        decipher.final(),
      ]).toString("utf8");
    } catch {
      return null;
    }
  }
}
