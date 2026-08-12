import { createHmac } from "node:crypto";

import { describe, expect, it } from "vitest";

import {
  clearLoginCookie,
  clearSessionCookie,
  constantTimeEqual,
  loginCookie,
  loginCookieName,
  randomOpaque,
  readOpaqueCookie,
  sessionCookie,
  sessionCookieName,
  StateSigner,
} from "./security";
import { AuthError } from "./types";

const opaque = Buffer.alloc(32, 7).toString("base64url");
const secret = new Uint8Array(Buffer.alloc(32, 11));

function signedPayload(value: unknown): string {
  const payload = Buffer.from(JSON.stringify(value), "utf8").toString(
    "base64url",
  );
  const signature = createHmac("sha256", secret)
    .update(payload)
    .digest("base64url");
  return `${payload}.${signature}`;
}

describe("browser auth security primitives", () => {
  it("generates opaque identifiers from the provided entropy source", () => {
    expect(
      randomOpaque({ bytes: () => new Uint8Array(Buffer.alloc(32, 7)) }),
    ).toBe(opaque);
  });

  it("compares equal values without accepting length or content changes", () => {
    expect(constantTimeEqual("same", "same")).toBe(true);
    expect(constantTimeEqual("same", "different-length")).toBe(false);
    expect(constantTimeEqual("same", "diff")).toBe(false);
  });

  it("signs, verifies, expires, and rejects malformed state", () => {
    const signer = new StateSigner(secret, 600);
    const state = signer.sign(opaque, 1_000);
    expect(signer.verify(state, 1_100)).toEqual({
      id: opaque,
      issuedAt: 1_000,
    });

    const cases = [
      "missing-separator",
      `${state}.extra`,
      `${state.slice(0, -1)}x`,
      `${Buffer.from("not json").toString("base64url")}.${state.split(".")[1]}`,
    ];
    for (const value of cases) {
      expect(() => signer.verify(value, 1_100)).toThrow(AuthError);
    }
    expect(() => signer.verify(state, 1_601)).toThrow(AuthError);
    expect(() => signer.verify(state, 969)).toThrow(AuthError);
    expect(() => signer.sign("short", 1_000)).toThrow(AuthError);
    expect(() => signer.sign(opaque, Number.NaN)).toThrow(AuthError);
  });

  it("rejects validly signed state with an unexpected payload schema", () => {
    const signer = new StateSigner(secret, 600);
    for (const value of [
      null,
      "text",
      [],
      { id: opaque, iat: 1_000, v: 2 },
      { id: opaque, iat: 1_000, v: 1, extra: true },
      { id: 1, iat: 1_000, v: 1 },
      { id: "short", iat: 1_000, v: 1 },
      { id: opaque, iat: "1000", v: 1 },
      { id: opaque, iat: 1_000.5, v: 1 },
    ]) {
      expect(() => signer.verify(signedPayload(value), 1_001)).toThrow(
        AuthError,
      );
    }
    expect(() =>
      signer.verify(signedPayload({ id: opaque, iat: 1_000, v: 1 }), 1.5),
    ).toThrow(AuthError);
  });

  it("reads one strict opaque cookie and rejects ambiguity", () => {
    expect(
      readOpaqueCookie(
        new Request("https://web.example", {
          headers: { cookie: `a=1; ${sessionCookieName}=${opaque}; b=2` },
        }),
        sessionCookieName,
      ),
    ).toBe(opaque);
    expect(
      readOpaqueCookie(
        new Request("https://web.example", {
          headers: {
            cookie: `${sessionCookieName}=${opaque}; ${sessionCookieName}=${opaque}`,
          },
        }),
        sessionCookieName,
      ),
    ).toBeNull();
    expect(
      readOpaqueCookie(
        new Request("https://web.example", {
          headers: { cookie: `${sessionCookieName}=` },
        }),
        sessionCookieName,
      ),
    ).toBeNull();
    expect(
      readOpaqueCookie(
        new Request("https://web.example", {
          headers: { cookie: `${sessionCookieName}=short; malformed` },
        }),
        sessionCookieName,
      ),
    ).toBeNull();
    expect(
      readOpaqueCookie(new Request("https://web.example"), sessionCookieName),
    ).toBeNull();
    expect(
      readOpaqueCookie(
        new Request("https://web.example", {
          headers: { cookie: "x=" + "a".repeat(8_193) },
        }),
        sessionCookieName,
      ),
    ).toBeNull();
  });

  it("emits strict __Host cookies and symmetric clearing directives", () => {
    for (const value of [
      sessionCookie(opaque, 600),
      loginCookie(opaque, 600),
    ]) {
      expect(value).toContain("Path=/");
      expect(value).toContain("Secure");
      expect(value).toContain("HttpOnly");
      expect(value).toContain("SameSite=Lax");
      expect(value).not.toContain("Domain=");
    }
    expect(sessionCookie(opaque, 600)).toMatch(
      new RegExp(`^${sessionCookieName}=`),
    );
    expect(loginCookie(opaque, 600)).toMatch(
      new RegExp(`^${loginCookieName}=`),
    );
    expect(clearSessionCookie()).toContain("Max-Age=0");
    expect(clearLoginCookie()).toContain("Max-Age=0");
    expect(() => sessionCookie("short", 1)).toThrow(TypeError);
    expect(() => sessionCookie(opaque, Number.NaN)).toThrow(TypeError);
    expect(() => loginCookie(opaque, -1)).toThrow(TypeError);
  });
});
