import { NextResponse } from "next/server";

import { authErrorResponse } from "@/lib/auth/http";
import { getAuthRuntime } from "@/lib/auth/runtime";
import { loginCookie } from "@/lib/auth/security";
import { AuthError } from "@/lib/auth/types";

export const dynamic = "force-dynamic";

export async function GET(request: Request): Promise<Response> {
  try {
    const url = new URL(request.url);
    const returnValues = url.searchParams.getAll("returnTo");
    if (returnValues.length > 1) {
      throw new AuthError("invalid_return_path", 400);
    }
    const start = await getAuthRuntime().service.beginLogin(
      returnValues[0] ?? "/",
    );
    const response = NextResponse.redirect(start.authorizationUrl, 302);
    response.headers.set("cache-control", "no-store");
    response.headers.append(
      "set-cookie",
      loginCookie(start.correlationId, start.maximumAge),
    );
    return response;
  } catch (error) {
    return authErrorResponse(error);
  }
}
