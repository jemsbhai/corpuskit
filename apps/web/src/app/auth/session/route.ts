import { NextResponse } from "next/server";

import { authErrorResponse } from "@/lib/auth/http";
import { getAuthRuntime } from "@/lib/auth/runtime";
import {
  clearSessionCookie,
  readOpaqueCookie,
  sessionCookie,
  sessionCookieName,
} from "@/lib/auth/security";

export const dynamic = "force-dynamic";

export async function GET(request: Request): Promise<Response> {
  try {
    const runtime = getAuthRuntime();
    const cookieId = readOpaqueCookie(request, sessionCookieName);
    let resolved = await runtime.service.resolveSession(cookieId);
    if (!resolved && runtime.service.config.mode !== "oidc") {
      resolved = await runtime.service.bootstrapLocalSession();
    }
    if (!resolved) {
      const response = NextResponse.json(
        { authenticated: false },
        {
          status: 200,
          headers: { "cache-control": "no-store", vary: "Cookie" },
        },
      );
      if (cookieId) response.headers.append("set-cookie", clearSessionCookie());
      return response;
    }
    const response = NextResponse.json(
      runtime.service.publicSession(resolved.value),
      {
        headers: { "cache-control": "no-store", vary: "Cookie" },
      },
    );
    response.headers.append(
      "set-cookie",
      sessionCookie(resolved.id, resolved.maximumAge),
    );
    return response;
  } catch (error) {
    return authErrorResponse(error);
  }
}
