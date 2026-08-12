import { NextResponse } from "next/server";

import { AuthError } from "./types";

const messages = {
  authentication_required: "Sign in is required for this request.",
  authentication_unavailable: "Authentication is temporarily unavailable.",
  csrf_validation_failed: "The request could not be verified.",
  invalid_authentication_callback:
    "The sign-in response is invalid or expired.",
  invalid_return_path: "The requested return location is not allowed.",
} as const;

export function authErrorResponse(reason: unknown): NextResponse {
  const error =
    reason instanceof AuthError
      ? reason
      : new AuthError("authentication_unavailable", 503);
  return NextResponse.json(
    { code: error.code, message: messages[error.code] },
    {
      status: error.status,
      headers: { "cache-control": "no-store" },
    },
  );
}
