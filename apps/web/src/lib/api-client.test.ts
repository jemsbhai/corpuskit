import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("@/lib/browser-auth", () => ({ authenticatedFetch: vi.fn() }));

import {
  ApiContractError,
  ApiRequestError,
  describeRequestError,
  isFiniteNumber,
  isNonnegativeInteger,
  isRecord,
  isStringArray,
  isUuid,
  pathIdentifier,
  postJson,
  queryString,
  requestDownload,
  requestJson,
  requestVoid,
} from "@/lib/api-client";
import { authenticatedFetch } from "@/lib/browser-auth";

const fetchMock = vi.mocked(authenticatedFetch);

afterEach(() => fetchMock.mockReset());

function json(value: unknown, init: ResponseInit = {}): Response {
  return new Response(JSON.stringify(value), {
    ...init,
    status: init.status ?? 200,
    headers: {
      "content-type": "application/json; charset=utf-8",
      ...init.headers,
    },
  });
}

describe("bounded API transport", () => {
  it("parses bounded JSON and sets safe JSON headers", async () => {
    fetchMock.mockResolvedValueOnce(json({ ok: true }));
    await expect(
      requestJson("/api/v1/check", (value) => value, postJson({ a: 1 })),
    ).resolves.toEqual({ ok: true });
    const init = fetchMock.mock.calls[0]?.[1];
    expect(new Headers(init?.headers).get("content-type")).toBe(
      "application/json",
    );
    expect(new Headers(init?.headers).get("accept")).toBe("application/json");
    expect(init?.cache).toBe("no-store");
  });

  it("does not force a JSON content type for multipart forms", async () => {
    fetchMock.mockResolvedValueOnce(json([]));
    await requestJson("/api/v1/upload", (value) => value, {
      method: "POST",
      body: new FormData(),
    });
    expect(
      new Headers(fetchMock.mock.calls[0]?.[1]?.headers).has("content-type"),
    ).toBe(false);
  });

  it.each([
    [401, "denied", "Sign in"],
    [403, "denied", "role"],
    [404, "missing", "not available"],
    [409, "conflict", "changed"],
    [413, "large", "size limit"],
    [422, "bad", "highlighted"],
    [400, "invalid_request", "highlighted"],
    [429, "busy", "busy"],
    [503, "down", "temporarily"],
    [400, "bad", "could not be completed"],
  ])("maps HTTP %i to stable public text", async (status, code, message) => {
    fetchMock.mockResolvedValueOnce(
      json(
        { code, request_id: "request-safe", message: "SECRET upstream" },
        { status },
      ),
    );
    const error = await requestJson("/api/v1/fail", (value) => value).catch(
      (caught: unknown) => caught,
    );
    expect(error).toBeInstanceOf(ApiRequestError);
    expect((error as Error).message).toContain(message);
    expect((error as Error).message).not.toContain("SECRET");
    expect(describeRequestError(error)).toContain("Reference request-safe");
  });

  it("uses a response request ID and fallback code when the error body is malformed", async () => {
    fetchMock.mockResolvedValueOnce(
      new Response("not-json", {
        status: 500,
        headers: { "x-request-id": "header-id" },
      }),
    );
    const error = (await requestJson("/api/v1/fail", (value) => value).catch(
      (caught: unknown) => caught,
    )) as ApiRequestError;
    expect(error.code).toBe("request_failed");
    expect(error.requestId).toBe("header-id");
  });

  it("rejects wrong media types, malformed JSON, invalid UTF-8, and parser failures", async () => {
    fetchMock
      .mockResolvedValueOnce(
        new Response("{}", { headers: { "content-type": "text/html" } }),
      )
      .mockResolvedValueOnce(
        new Response("{", { headers: { "content-type": "application/json" } }),
      )
      .mockResolvedValueOnce(
        new Response(Uint8Array.of(0xff), {
          headers: { "content-type": "application/json" },
        }),
      )
      .mockResolvedValueOnce(json({ ok: false }));
    await expect(
      requestJson("/api/v1/a", (value) => value),
    ).rejects.toBeInstanceOf(ApiContractError);
    await expect(
      requestJson("/api/v1/b", (value) => value),
    ).rejects.toBeInstanceOf(ApiContractError);
    await expect(
      requestJson("/api/v1/c", (value) => value),
    ).rejects.toBeInstanceOf(ApiContractError);
    await expect(
      requestJson("/api/v1/d", () => {
        throw new TypeError("private");
      }),
    ).rejects.toBeInstanceOf(ApiContractError);
  });

  it("rejects declared and chunked oversized responses and invalid lengths", async () => {
    fetchMock
      .mockResolvedValueOnce(
        new Response("{}", {
          headers: {
            "content-type": "application/json",
            "content-length": "10485761",
          },
        }),
      )
      .mockResolvedValueOnce(
        new Response("{}", {
          headers: {
            "content-type": "application/json",
            "content-length": "-1",
          },
        }),
      )
      .mockResolvedValueOnce(
        new Response(
          new ReadableStream({
            start(controller) {
              controller.enqueue(new Uint8Array(10 * 1024 * 1024));
              controller.enqueue(Uint8Array.of(1));
              controller.close();
            },
          }),
          { headers: { "content-type": "application/json" } },
        ),
      );
    await expect(
      requestJson("/api/v1/a", (value) => value),
    ).rejects.toBeInstanceOf(ApiContractError);
    await expect(
      requestJson("/api/v1/b", (value) => value),
    ).rejects.toBeInstanceOf(ApiContractError);
    await expect(
      requestJson("/api/v1/c", (value) => value),
    ).rejects.toBeInstanceOf(ApiContractError);
  });

  it("downloads bounded bytes and enforces empty mutation responses", async () => {
    fetchMock
      .mockResolvedValueOnce(
        new Response(Uint8Array.of(1, 2, 3), { headers: { "x-check": "yes" } }),
      )
      .mockResolvedValueOnce(new Response(null, { status: 204 }))
      .mockResolvedValueOnce(new Response("unexpected", { status: 200 }));
    const result = await requestDownload("/api/v1/file");
    expect(Array.from(result.bytes)).toEqual([1, 2, 3]);
    expect(result.headers.get("x-check")).toBe("yes");
    await expect(
      requestVoid("/api/v1/file", { method: "DELETE" }),
    ).resolves.toBeUndefined();
    await expect(
      requestVoid("/api/v1/file", { method: "DELETE" }),
    ).rejects.toBeInstanceOf(ApiContractError);
  });
});

describe("transport validators", () => {
  it("validates primitives, paths, and queries", () => {
    expect(isRecord({})).toBe(true);
    expect(isRecord([])).toBe(false);
    expect(isRecord(null)).toBe(false);
    expect(isStringArray(["a"])).toBe(true);
    expect(isStringArray([1])).toBe(false);
    expect(isFiniteNumber(1)).toBe(true);
    expect(isFiniteNumber(Infinity)).toBe(false);
    expect(isNonnegativeInteger(0)).toBe(true);
    expect(isNonnegativeInteger(-1)).toBe(false);
    expect(isUuid("123e4567-e89b-42d3-a456-426614174000")).toBe(true);
    expect(isUuid("nope")).toBe(false);
    expect(pathIdentifier("safe_id-1.0")).toBe("safe_id-1.0");
    expect(() => pathIdentifier("../escape")).toThrow(TypeError);
    expect(
      queryString({ q: "a b", page: 2, enabled: false, empty: "", none: null }),
    ).toBe("?q=a+b&page=2&enabled=false");
    expect(queryString({})).toBe("");
    expect(describeRequestError(new Error("secret"))).not.toContain("secret");
  });
});
