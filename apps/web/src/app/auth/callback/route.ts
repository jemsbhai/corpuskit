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

export async function GET(request: Request): Promise<Response> {
  if (request.url.length > 8_192) {
    const response = authErrorResponse(
      new AuthError("invalid_authentication_callback", 400),
    );
    response.headers.append("set-cookie", clearLoginCookie());
    return response;
  }

  try {
    const runtime = getAuthRuntime();
    const result = await runtime.service.completeLogin(
      new URL(request.url),
      readOpaqueCookie(request, loginCookieName),
      readOpaqueCookie(request, sessionCookieName),
    );
    const redirectBase = runtime.service.config.redirectUri;
    if (!redirectBase) {
      throw new AuthError("authentication_unavailable", 503);
    }
    const response = NextResponse.redirect(
      new URL(result.returnPath, redirectBase.origin),
      303,
    );
    response.headers.set("cache-control", "no-store");
    response.headers.append("set-cookie", clearLoginCookie());
    response.headers.append(
      "set-cookie",
      sessionCookie(result.sessionId, result.maximumAge),
    );
    return response;
  } catch (error) {
    const response = authErrorResponse(error);
    response.headers.append("set-cookie", clearLoginCookie());
    return response;
  }
}
