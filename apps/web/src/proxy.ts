import { NextResponse, type NextRequest } from "next/server";

export function pageContentSecurityPolicy(
  nonce: string,
  development = process.env.NODE_ENV === "development",
  enforceHttps = process.env.CORPUSKIT_ENVIRONMENT !== "test",
): string {
  if (!/^[A-Za-z0-9+/]{48}$/u.test(nonce)) {
    throw new TypeError("Invalid CSP nonce.");
  }
  return [
    "default-src 'self'",
    "base-uri 'self'",
    `connect-src 'self'${development ? " ws: wss:" : ""}`,
    "font-src 'self'",
    "form-action 'self'",
    "frame-ancestors 'none'",
    "img-src 'self' data:",
    "manifest-src 'self'",
    "object-src 'none'",
    `script-src 'self' 'nonce-${nonce}' 'strict-dynamic'${development ? " 'unsafe-eval'" : ""}`,
    "style-src 'self' 'unsafe-inline'",
    ...(!development && enforceHttps ? ["upgrade-insecure-requests"] : []),
  ].join("; ");
}

export function proxy(request: NextRequest): NextResponse {
  const nonce = Buffer.from(crypto.randomUUID(), "utf8").toString("base64");
  const policy = pageContentSecurityPolicy(nonce);
  const requestHeaders = new Headers(request.headers);
  requestHeaders.set("content-security-policy", policy);
  requestHeaders.set("x-nonce", nonce);
  const response = NextResponse.next({ request: { headers: requestHeaders } });
  response.headers.set("content-security-policy", policy);
  return response;
}

export const config = {
  matcher: [
    {
      source:
        "/((?!api|auth|_next/static|_next/image|favicon.ico|robots.txt).*)",
      missing: [
        { type: "header", key: "next-router-prefetch" },
        { type: "header", key: "purpose", value: "prefetch" },
      ],
    },
  ],
};
