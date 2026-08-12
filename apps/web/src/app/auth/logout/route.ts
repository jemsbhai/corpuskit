import { authErrorResponse } from "@/lib/auth/http";
import { getAuthRuntime } from "@/lib/auth/runtime";
import {
  clearSessionCookie,
  csrfHeaderName,
  readOpaqueCookie,
  sessionCookieName,
} from "@/lib/auth/security";

export const dynamic = "force-dynamic";

export async function POST(request: Request): Promise<Response> {
  try {
    await getAuthRuntime().service.logout(
      readOpaqueCookie(request, sessionCookieName),
      request.headers.get(csrfHeaderName),
    );
    return new Response(null, {
      status: 204,
      headers: {
        "cache-control": "no-store",
        "set-cookie": clearSessionCookie(),
      },
    });
  } catch (error) {
    return authErrorResponse(error);
  }
}
