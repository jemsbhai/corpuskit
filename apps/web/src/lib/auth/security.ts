import {
  createHmac,
  randomBytes,
  timingSafeEqual as nodeTimingSafeEqual,
} from "node:crypto";

import { AuthError } from "./types";

export const sessionCookieName = "__Host-corpuskit_session";
export const loginCookieName = "__Host-corpuskit_login";
export const csrfHeaderName = "x-corpuskit-csrf";

const opaquePattern = /^[A-Za-z0-9_-]{43,128}$/u;

export interface RandomSource {
  bytes(size: number): Uint8Array;
}

export const secureRandom: RandomSource = {
  bytes: (size) => new Uint8Array(randomBytes(size)),
};

export function randomOpaque(source: RandomSource = secureRandom): string {
  return Buffer.from(source.bytes(32)).toString("base64url");
}

export function constantTimeEqual(left: string, right: string): boolean {
  const leftBytes = Buffer.from(left, "utf8");
  const rightBytes = Buffer.from(right, "utf8");
  if (leftBytes.byteLength !== rightBytes.byteLength) return false;
  return nodeTimingSafeEqual(leftBytes, rightBytes);
}

export class StateSigner {
  constructor(
    private readonly key: Uint8Array,
    private readonly maximumAgeSeconds: number,
  ) {}

  sign(id: string, issuedAt: number): string {
    if (!opaquePattern.test(id) || !Number.isSafeInteger(issuedAt)) {
      throw new AuthError("invalid_authentication_callback", 400);
    }
    const payload = Buffer.from(
      JSON.stringify({ id, iat: issuedAt, v: 1 }),
      "utf8",
    ).toString("base64url");
    const signature = this.signature(payload);
    return `${payload}.${signature}`;
  }

  verify(value: string, now: number): { id: string; issuedAt: number } {
    const parts = value.split(".");
    const payload = parts[0];
    const signature = parts[1];
    if (!payload || !signature || parts.length !== 2) this.reject();
    const expected = this.signature(payload);
    if (!constantTimeEqual(signature, expected)) this.reject();

    let decoded: unknown;
    try {
      decoded = JSON.parse(Buffer.from(payload, "base64url").toString("utf8"));
    } catch {
      this.reject();
    }
    if (!decoded || typeof decoded !== "object" || Array.isArray(decoded)) {
      this.reject();
    }
    const record = decoded as Record<string, unknown>;
    if (
      Object.keys(record).sort().join(",") !== "iat,id,v" ||
      record.v !== 1 ||
      typeof record.id !== "string" ||
      !opaquePattern.test(record.id) ||
      typeof record.iat !== "number" ||
      !Number.isSafeInteger(record.iat) ||
      !Number.isSafeInteger(now) ||
      record.iat > now + 30 ||
      record.iat < now - this.maximumAgeSeconds
    ) {
      this.reject();
    }
    return { id: record.id, issuedAt: record.iat };
  }

  private signature(payload: string): string {
    return createHmac("sha256", this.key).update(payload).digest("base64url");
  }

  private reject(): never {
    throw new AuthError("invalid_authentication_callback", 400);
  }
}

export function readOpaqueCookie(
  request: Request,
  name: string,
): string | null {
  const header = request.headers.get("cookie");
  if (!header || header.length > 8_192) return null;
  const matches: string[] = [];
  for (const part of header.split(";")) {
    const separator = part.indexOf("=");
    if (separator < 1) continue;
    if (part.slice(0, separator).trim() !== name) continue;
    matches.push(part.slice(separator + 1).trim());
  }
  if (matches.length !== 1) return null;
  const value = matches[0];
  if (!value || !opaquePattern.test(value)) return null;
  return value;
}

function cookie(name: string, value: string, maximumAge: number): string {
  if (
    !opaquePattern.test(value) ||
    !Number.isSafeInteger(maximumAge) ||
    maximumAge < 0
  ) {
    throw new TypeError("Invalid cookie value.");
  }
  return `${name}=${value}; Path=/; Max-Age=${maximumAge}; Secure; HttpOnly; SameSite=Lax; Priority=High`;
}

export function sessionCookie(value: string, maximumAge: number): string {
  return cookie(sessionCookieName, value, maximumAge);
}

export function loginCookie(value: string, maximumAge: number): string {
  return cookie(loginCookieName, value, maximumAge);
}

export function clearSessionCookie(): string {
  return `${sessionCookieName}=; Path=/; Max-Age=0; Secure; HttpOnly; SameSite=Lax; Priority=High`;
}

export function clearLoginCookie(): string {
  return `${loginCookieName}=; Path=/; Max-Age=0; Secure; HttpOnly; SameSite=Lax; Priority=High`;
}
