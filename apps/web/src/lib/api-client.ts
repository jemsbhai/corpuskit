import { authenticatedFetch } from "@/lib/browser-auth";

const maximumJsonBytes = 10 * 1024 * 1024;
const maximumErrorBytes = 64 * 1024;

export type JsonRecord = Record<string, unknown>;

export class ApiRequestError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    readonly requestId: string | null,
  ) {
    super(publicMessage(status, code));
    this.name = "ApiRequestError";
  }
}

export class ApiContractError extends Error {
  constructor() {
    super("The service returned an incompatible response.");
    this.name = "ApiContractError";
  }
}

export function isRecord(value: unknown): value is JsonRecord {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

export function isStringArray(value: unknown): value is string[] {
  return (
    Array.isArray(value) && value.every((item) => typeof item === "string")
  );
}

export function isFiniteNumber(value: unknown): value is number {
  return typeof value === "number" && Number.isFinite(value);
}

export function isNonnegativeInteger(value: unknown): value is number {
  return Number.isSafeInteger(value) && (value as number) >= 0;
}

export function isUuid(value: unknown): value is string {
  return (
    typeof value === "string" &&
    /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/iu.test(
      value,
    )
  );
}

export function pathIdentifier(value: string): string {
  if (!/^[A-Za-z0-9][A-Za-z0-9._~-]{0,127}$/u.test(value)) {
    throw new TypeError("Invalid path identifier.");
  }
  return encodeURIComponent(value);
}

export function queryString(
  values: Readonly<
    Record<string, string | number | boolean | null | undefined>
  >,
): string {
  const query = new URLSearchParams();
  for (const [key, value] of Object.entries(values)) {
    if (value !== undefined && value !== null && value !== "") {
      query.set(key, String(value));
    }
  }
  const encoded = query.toString();
  return encoded ? `?${encoded}` : "";
}

export async function requestJson<T>(
  path: string,
  parse: (value: unknown) => T,
  init: RequestInit = {},
): Promise<T> {
  const headers = new Headers(init.headers);
  headers.set("accept", "application/json");
  if (init.body !== undefined && !(init.body instanceof FormData)) {
    headers.set("content-type", "application/json");
  }
  const response = await authenticatedFetch(path, {
    cache: "no-store",
    ...init,
    headers,
  });
  if (!response.ok) throw await parseApiError(response);
  const contentType = response.headers.get("content-type") ?? "";
  if (!contentType.toLowerCase().startsWith("application/json")) {
    throw new ApiContractError();
  }
  const value = await boundedJson(response, maximumJsonBytes);
  try {
    return parse(value);
  } catch (error) {
    if (error instanceof ApiRequestError) throw error;
    throw new ApiContractError();
  }
}

export function postJson(body: unknown, signal?: AbortSignal): RequestInit {
  return {
    method: "POST",
    body: JSON.stringify(body),
    ...(signal ? { signal } : {}),
  };
}

export async function requestDownload(
  path: string,
  init: RequestInit = {},
): Promise<{ readonly bytes: Uint8Array; readonly headers: Headers }> {
  const response = await authenticatedFetch(path, {
    cache: "no-store",
    ...init,
  });
  if (!response.ok) throw await parseApiError(response);
  return {
    bytes: new Uint8Array(await boundedBytes(response, maximumJsonBytes)),
    headers: response.headers,
  };
}

export async function requestVoid(
  path: string,
  init: RequestInit = {},
): Promise<void> {
  const response = await authenticatedFetch(path, {
    cache: "no-store",
    ...init,
  });
  if (!response.ok) throw await parseApiError(response);
  if (response.body) {
    const bytes = await boundedBytes(response, maximumErrorBytes);
    if (bytes.byteLength !== 0) throw new ApiContractError();
  }
}

export function describeRequestError(error: unknown): string {
  if (error instanceof ApiRequestError || error instanceof ApiContractError) {
    const reference =
      error instanceof ApiRequestError && error.requestId
        ? ` Reference ${error.requestId}.`
        : "";
    return `${error.message}${reference}`;
  }
  return "The service is temporarily unavailable. No result was substituted.";
}

async function parseApiError(response: Response): Promise<ApiRequestError> {
  let value: unknown = null;
  try {
    value = await boundedJson(response, maximumErrorBytes);
  } catch {
    // The stable status fallback below intentionally hides malformed details.
  }
  const code =
    isRecord(value) && typeof value.code === "string"
      ? value.code.slice(0, 128)
      : "request_failed";
  const requestId =
    isRecord(value) && typeof value.request_id === "string"
      ? value.request_id.slice(0, 128)
      : response.headers.get("x-request-id");
  return new ApiRequestError(response.status, code, requestId);
}

function publicMessage(status: number, code: string): string {
  if (status === 401) return "Sign in is required for this operation.";
  if (status === 403) return "Your current role cannot perform this operation.";
  if (status === 404)
    return "The requested resource or operation is not available.";
  if (status === 409)
    return "The resource changed before the operation completed.";
  if (status === 413) return "The request exceeds the configured size limit.";
  if (status === 422 || code === "invalid_request") {
    return "Review the highlighted inputs and try again.";
  }
  if (status === 429) return "The service is busy. Wait before trying again.";
  if (status >= 500) return "The service is temporarily unavailable.";
  return "The request could not be completed.";
}

async function boundedJson(
  response: Response,
  maximum: number,
): Promise<unknown> {
  const bytes = await boundedBytes(response, maximum);
  let text: string;
  try {
    text = new TextDecoder("utf-8", { fatal: true }).decode(bytes);
  } catch {
    throw new ApiContractError();
  }
  try {
    return JSON.parse(text) as unknown;
  } catch {
    throw new ApiContractError();
  }
}

async function boundedBytes(
  response: Response,
  maximum: number,
): Promise<ArrayBuffer> {
  const rawLength = response.headers.get("content-length");
  if (rawLength !== null) {
    if (!/^(?:0|[1-9][0-9]*)$/u.test(rawLength)) throw new ApiContractError();
    const declared = Number(rawLength);
    if (!Number.isSafeInteger(declared) || declared > maximum) {
      throw new ApiContractError();
    }
  }
  if (!response.body) return new ArrayBuffer(0);
  const reader = response.body.getReader();
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
        // The oversized response remains rejected when cancellation fails.
      }
      throw new ApiContractError();
    }
    chunks.push(part.value);
  }
  const bytes = new Uint8Array(total);
  let offset = 0;
  for (const chunk of chunks) {
    bytes.set(chunk, offset);
    offset += chunk.byteLength;
  }
  return bytes.buffer;
}
