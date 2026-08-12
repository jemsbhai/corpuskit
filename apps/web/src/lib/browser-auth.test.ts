import { afterEach, describe, expect, it, vi } from "vitest";

import {
  authenticatedFetch,
  browserSession,
  clearBrowserSessionCache,
} from "./browser-auth";

const csrf = Buffer.alloc(32, 55).toString("base64url");

function sessionResponse(overrides: Record<string, unknown> = {}) {
  return Response.json({
    authenticated: true,
    csrfToken: csrf,
    subject: "user-1",
    displayName: "User One",
    expiresAt: new Date(Date.now() + 60_000).toISOString(),
    ...overrides,
  });
}

afterEach(() => {
  clearBrowserSessionCache();
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

describe("browser auth fetch boundary", () => {
  it("bootstraps one session before safe calls and strips browser bearer headers", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(sessionResponse())
      .mockResolvedValueOnce(Response.json({ ok: true }));
    vi.stubGlobal("fetch", fetchMock);
    await authenticatedFetch("/api/v1/capabilities", {
      headers: {
        authorization: "Bearer browser-token",
        accept: "application/json",
      },
    });
    expect(fetchMock.mock.calls[0]?.[0]).toBe("/auth/session");
    expect(fetchMock).toHaveBeenCalledTimes(2);
    const init = fetchMock.mock.calls[1]?.[1] as RequestInit;
    expect(init.credentials).toBe("same-origin");
    expect(new Headers(init.headers).get("authorization")).toBeNull();
    expect(new Headers(init.headers).get("accept")).toBe("application/json");
  });

  it("serializes concurrent first-load reads behind one session bootstrap", async () => {
    let resolveSession: ((response: Response) => void) | undefined;
    const pendingSession = new Promise<Response>((resolve) => {
      resolveSession = resolve;
    });
    const fetchMock = vi
      .fn()
      .mockImplementationOnce(() => pendingSession)
      .mockResolvedValue(Response.json({ ok: true }));
    vi.stubGlobal("fetch", fetchMock);

    const first = authenticatedFetch("/api/v1/auth/me");
    const second = authenticatedFetch("/api/v1/projects");
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(fetchMock.mock.calls[0]?.[0]).toBe("/auth/session");

    resolveSession?.(sessionResponse());
    await expect(Promise.all([first, second])).resolves.toHaveLength(2);
    expect(fetchMock).toHaveBeenCalledTimes(3);
    expect(fetchMock.mock.calls.slice(1).map(([input]) => input)).toEqual([
      "/api/v1/auth/me",
      "/api/v1/projects",
    ]);
  });

  it("loads one server session and binds its CSRF value to mutations", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(sessionResponse())
      .mockResolvedValueOnce(Response.json({ ok: true }))
      .mockResolvedValueOnce(Response.json({ ok: true }));
    vi.stubGlobal("fetch", fetchMock);
    await authenticatedFetch("/api/v1/evaluations", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: "{}",
    });
    await authenticatedFetch("/api/v1/projects", {
      method: "POST",
      body: "{}",
    });
    expect(fetchMock.mock.calls[0]?.[0]).toBe("/auth/session");
    for (const index of [1, 2]) {
      const headers = new Headers(fetchMock.mock.calls[index]?.[1]?.headers);
      expect(headers.get("x-corpuskit-csrf")).toBe(csrf);
    }
    expect(fetchMock).toHaveBeenCalledTimes(3);
  });

  it("does not retry a mutation when the CSRF-protected request fails", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(sessionResponse())
      .mockResolvedValueOnce(
        Response.json({ code: "denied" }, { status: 403 }),
      );
    vi.stubGlobal("fetch", fetchMock);
    const response = await authenticatedFetch("/api/v1/projects", {
      method: "DELETE",
    });
    expect(response.status).toBe(403);
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("fails before mutation on missing, expired, malformed, or oversized session data", async () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2030-01-01T00:00:00Z"));
    const responses = [
      Response.json({ authenticated: false }),
      sessionResponse({ expiresAt: "2029-01-01T00:00:00.000Z" }),
      new Response("not-json"),
      new Response("x".repeat(8_193)),
      new Response("{}", { status: 503 }),
      new Response("{}", { headers: { "content-length": "8193" } }),
    ];
    for (const response of responses) {
      clearBrowserSessionCache();
      const fetchMock = vi.fn().mockResolvedValue(response);
      vi.stubGlobal("fetch", fetchMock);
      await expect(
        authenticatedFetch("/api/v1/projects", { method: "POST" }),
      ).rejects.toThrow();
      expect(fetchMock).toHaveBeenCalledTimes(1);
    }
  });

  it("clears failed session lookups for a later explicit retry", async () => {
    const fetchMock = vi
      .fn()
      .mockRejectedValueOnce(new Error("outage"))
      .mockResolvedValueOnce(sessionResponse());
    vi.stubGlobal("fetch", fetchMock);
    await expect(browserSession()).rejects.toThrow("outage");
    await expect(browserSession()).resolves.toMatchObject({
      subject: "user-1",
    });
  });

  it("cancels a chunked session response at the byte cap before mutation", async () => {
    let cancelled = false;
    const response = new Response(
      new ReadableStream<Uint8Array>({
        start(controller) {
          controller.enqueue(new Uint8Array(5_000));
          controller.enqueue(new Uint8Array(5_000));
        },
        cancel() {
          cancelled = true;
        },
      }),
    );
    const fetchMock = vi.fn().mockResolvedValue(response);
    vi.stubGlobal("fetch", fetchMock);
    await expect(
      authenticatedFetch("/api/v1/projects", { method: "POST" }),
    ).rejects.toThrow("exceeded its limit");
    expect(cancelled).toBe(true);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("rejects external, protocol-relative, backslash, and unapproved destinations before token lookup", async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    await expect(
      authenticatedFetch("https://evil.example/collect", { method: "POST" }),
    ).rejects.toThrow(TypeError);
    await expect(
      authenticatedFetch("//evil.example/collect", { method: "POST" }),
    ).rejects.toThrow(TypeError);
    await expect(
      authenticatedFetch("/\\\\evil.example/collect", { method: "POST" }),
    ).rejects.toThrow(TypeError);
    await expect(
      authenticatedFetch("/unapproved", { method: "POST" }),
    ).rejects.toThrow(TypeError);
    await expect(
      authenticatedFetch("/api/v1/projects#secret", { method: "POST" }),
    ).rejects.toThrow(TypeError);
    expect(fetchMock).not.toHaveBeenCalled();
  });
});
