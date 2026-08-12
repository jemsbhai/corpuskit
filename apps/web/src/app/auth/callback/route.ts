import { NextResponse } from "next/server";

import { authErrorResponse } from "@/lib/auth/http";
import { getAuthRuntime } from "@/lib/auth/runtime";
import {
  clearLoginCookie,
  loginCookieName,
  readOpaqueCookie,
  sessionCookie,
  sessionCookieName,
} from "@/lib/auth/security";
import { AuthError } from "@/lib/auth/types";

export const dynamic = "force-dynamic";

function boundedCallbackUrl(rawUrl: string): URL {
  const candidate =
    rawUrl.length > 8_192 ? "https://invalid.invalid/auth/callback" : rawUrl;
  return new URL(candidate);
}

export async function GET(request: Request): Promise<Response> {
  let response: Response;
  try {
    const runtime = getAuthRuntime();
    const result = await runtime.service.completeLogin(
      boundedCallbackUrl(request.url),
      readOpaqueCookie(request, loginCookieName),
      readOpaqueCookie(request, sessionCookieName),
    );
    const redirectBase = runtime.service.config.redirectUri;
    if (!redirectBase) {
      throw new AuthError("authentication_unavailable", 503);
    }
    response = NextResponse.redirect(
      new URL(result.returnPath, redirectBase.origin),
      303,
    );
    response.headers.set("cache-control", "no-store");
    response.headers.append(
      "set-cookie",
      sessionCookie(result.sessionId, result.maximumAge),
    );
  } catch (error) {
    response = authErrorResponse(error);
  }
  response.headers.append("set-cookie", clearLoginCookie());
  return response;
}
