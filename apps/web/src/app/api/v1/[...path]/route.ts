import { NextResponse } from "next/server";

import { getAuthRuntime } from "@/lib/auth/runtime";
import {
  csrfHeaderName,
  readOpaqueCookie,
  sessionCookie,
  sessionCookieName,
} from "@/lib/auth/security";
import { AuthError } from "@/lib/auth/types";

export const dynamic = "force-dynamic";

type RouteContext = { params: Promise<{ path: string[] }> };

const forwardedRequestHeaders = [
  "accept",
  "content-type",
  "x-request-id",
  "idempotency-key",
] as const;
const forwardedResponseHeaders = [
  "content-type",
  "x-request-id",
  "retry-after",
  "www-authenticate",
  "ratelimit-limit",
  "ratelimit-remaining",
  "ratelimit-reset",
  "content-disposition",
  "content-digest",
  "etag",
  "x-content-sha256",
] as const;
const maximumBodyBytes = 10 * 1024 * 1024;
const maximumResponseBytes = 10 * 1024 * 1024;
const maximumSearchCharacters = 4_096;
const requestIdPattern = /^[A-Za-z0-9._-]{1,128}$/u;
const idempotencyKeyPattern = /^[A-Za-z0-9._:-]{1,128}$/u;
const contentDispositionPattern =
  /^attachment; filename="[A-Za-z0-9._-]{1,128}"; filename\*=UTF-8''[A-Za-z0-9._~%+-]{1,1024}$/u;
const contentDigestPattern = /^sha-256=:[A-Za-z0-9+/]{43}=:$/u;
const etagPattern = /^"[0-9a-f]{64}"$/u;
const sha256Pattern = /^[0-9a-f]{64}$/u;
const pathSegmentPattern = /^[A-Za-z0-9][A-Za-z0-9._~-]{0,127}$/u;

function safeResponseHeader(name: string, value: string): boolean {
  if (name === "content-disposition") {
    return contentDispositionPattern.test(value);
  }
  if (name === "content-digest") return contentDigestPattern.test(value);
  if (name === "etag") return etagPattern.test(value);
  if (name === "x-content-sha256") return sha256Pattern.test(value);
  return true;
}

function safeRequestId(request: Request): string {
  const candidate = request.headers.get("x-request-id");
  return candidate && requestIdPattern.test(candidate)
    ? candidate
    : crypto.randomUUID();
}

function errorResponse(
  request: Request,
  status: number,
  code: string,
  message: string,
) {
  const requestId = safeRequestId(request);
  return NextResponse.json(
    { code, message, operation: "web.api_proxy", request_id: requestId },
    {
      status,
      headers: { "cache-control": "no-store", "x-request-id": requestId },
    },
  );
}

function upstreamBase(): URL | null {
  const environment = process.env.CORPUSKIT_ENVIRONMENT;
  const productionLike =
    environment === "production" || environment === "staging";
  const configured = process.env.CORPUSKIT_API_INTERNAL_URL;
  if (!configured && productionLike) return null;
  try {
    const url = new URL(
      (configured ?? "http://127.0.0.1:8000/api/v1").replace(/\/?$/u, "/"),
    );
    if (
      (url.protocol !== "http:" && url.protocol !== "https:") ||
      !url.hostname ||
      url.username ||
      url.password ||
      url.search ||
      url.hash ||
      url.pathname !== "/api/v1/"
    ) {
      return null;
    }
    return url;
  } catch {
    return null;
  }
}

function isPublicPath(path: readonly string[], method: string): boolean {
  if (method !== "GET" && method !== "HEAD") return false;
  const value = path.join("/");
  return (
    value === "capabilities" ||
    value === "health/live" ||
    value === "health/ready" ||
    value === "version"
  );
}

function validDeclaredLength(headers: Headers, maximum: number): boolean {
  const raw = headers.get("content-length");
  if (raw === null) return true;
  if (!/^(?:0|[1-9][0-9]*)$/u.test(raw)) return false;
  const value = Number(raw);
  return Number.isSafeInteger(value) && value <= maximum;
}

async function boundedBody(
  body: ReadableStream<Uint8Array> | null,
  maximum: number,
): Promise<ArrayBuffer | null> {
  if (!body) return null;
  const reader = body.getReader();
  const chunks: Uint8Array[] = [];
  let total = 0;
  while (true) {
    const part = await reader.read();
    if (part.done) break;
    total += part.value.byteLength;
    if (total > maximum) {
      try {
        await reader.cancel();
      } catch {
        // The payload is rejected even when transport cancellation fails.
      }
      throw new RangeError("Upstream response exceeded the proxy limit.");
    }
    chunks.push(part.value);
  }
  const result = new Uint8Array(new ArrayBuffer(total));
  let offset = 0;
  for (const chunk of chunks) {
    result.set(chunk, offset);
    offset += chunk.byteLength;
  }
  return result.buffer;
}

async function proxy(
  request: Request,
  context: RouteContext,
): Promise<Response> {
  const base = upstreamBase();
  if (!base) {
    return errorResponse(
      request,
      503,
      "api_proxy_unavailable",
      "The API proxy is not configured for this deployment.",
    );
  }

  const { path } = await context.params;
  if (
    path.length === 0 ||
    path.length > 10 ||
    path.some(
      (segment) =>
        !pathSegmentPattern.test(segment) ||
        segment === "." ||
        segment === "..",
    )
  ) {
    return errorResponse(
      request,
      404,
      "api_path_not_found",
      "The requested API path is not available.",
    );
  }
  const encodedPath = path
    .map((segment) => encodeURIComponent(segment))
    .join("/");
  const upstream = new URL(encodedPath, base);
  const search = new URL(request.url).search;
  if (search.length > maximumSearchCharacters) {
    return errorResponse(
      request,
      414,
      "request_uri_too_long",
      "The request query exceeds the web proxy limit.",
    );
  }
  upstream.search = search;

  let resolvedSession;
  try {
    const runtime = getAuthRuntime();
    const cookieId = readOpaqueCookie(request, sessionCookieName);
    resolvedSession = await runtime.service.resolveSession(cookieId);
    if (!resolvedSession && runtime.service.config.mode !== "oidc") {
      if (request.method === "GET" || request.method === "HEAD") {
        resolvedSession = await runtime.service.bootstrapLocalSession();
      }
    }
    if (!resolvedSession && !isPublicPath(path, request.method)) {
      throw new AuthError("authentication_required", 401);
    }
    if (
      resolvedSession &&
      request.method !== "GET" &&
      request.method !== "HEAD" &&
      request.method !== "OPTIONS" &&
      !runtime.service.csrfMatches(
        resolvedSession.value,
        request.headers.get(csrfHeaderName),
      )
    ) {
      throw new AuthError("csrf_validation_failed", 403);
    }
  } catch (error) {
    const authError =
      error instanceof AuthError
        ? error
        : new AuthError("authentication_unavailable", 503);
    return errorResponse(
      request,
      authError.status,
      authError.code,
      authError.code === "csrf_validation_failed"
        ? "The request could not be verified."
        : authError.code === "authentication_required"
          ? "Sign in is required for this request."
          : "Authentication is temporarily unavailable.",
    );
  }

  const headers = new Headers();
  for (const name of forwardedRequestHeaders) {
    const value = request.headers.get(name);
    if (
      value &&
      (name !== "idempotency-key" || idempotencyKeyPattern.test(value))
    ) {
      headers.set(name, value);
    }
  }
  if (resolvedSession?.value.accessToken) {
    headers.set("authorization", `Bearer ${resolvedSession.value.accessToken}`);
  }

  let response: Response;
  try {
    let body: ArrayBuffer | undefined;
    if (request.method !== "GET" && request.method !== "HEAD") {
      if (!validDeclaredLength(request.headers, maximumBodyBytes)) {
        return errorResponse(
          request,
          413,
          "request_too_large",
          "The request exceeds the web proxy upload limit.",
        );
      }
      try {
        body = (await boundedBody(request.body, maximumBodyBytes)) ?? undefined;
      } catch {
        return errorResponse(
          request,
          413,
          "request_too_large",
          "The request exceeds the web proxy upload limit.",
        );
      }
    }
    response = await fetch(upstream, {
      method: request.method,
      headers,
      body,
      cache: "no-store",
      redirect: "manual",
      signal: AbortSignal.any([request.signal, AbortSignal.timeout(30_000)]),
    });
  } catch {
    return errorResponse(
      request,
      503,
      "api_upstream_unavailable",
      "The CorpusKit API is temporarily unavailable.",
    );
  }

  if (!validDeclaredLength(response.headers, maximumResponseBytes)) {
    return errorResponse(
      request,
      502,
      "api_response_too_large",
      "The API response exceeds the web proxy limit.",
    );
  }

  let responseBody: ArrayBuffer | null;
  try {
    responseBody = await boundedBody(response.body, maximumResponseBytes);
  } catch {
    return errorResponse(
      request,
      502,
      "api_response_too_large",
      "The API response exceeds the web proxy limit.",
    );
  }

  const responseHeaders = new Headers();
  responseHeaders.set("cache-control", "no-store");
  for (const name of forwardedResponseHeaders) {
    const value = response.headers.get(name);
    if (value && safeResponseHeader(name, value)) {
      responseHeaders.set(name, value);
    }
  }
  if (resolvedSession) {
    responseHeaders.append(
      "set-cookie",
      sessionCookie(resolvedSession.id, resolvedSession.maximumAge),
    );
  }
  return new Response(responseBody, {
    status: response.status,
    headers: responseHeaders,
  });
}

export function GET(request: Request, context: RouteContext) {
  return proxy(request, context);
}

export function POST(request: Request, context: RouteContext) {
  return proxy(request, context);
}

export function PUT(request: Request, context: RouteContext) {
  return proxy(request, context);
}

export function PATCH(request: Request, context: RouteContext) {
  return proxy(request, context);
}

export function DELETE(request: Request, context: RouteContext) {
  return proxy(request, context);
}
