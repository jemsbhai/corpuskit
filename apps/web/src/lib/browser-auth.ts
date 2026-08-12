const csrfPattern = /^[A-Za-z0-9_-]{43,128}$/u;
const csrfHeaderName = "x-corpuskit-csrf";
const maximumSessionResponseCharacters = 8_192;

export interface BrowserSessionView {
  readonly authenticated: true;
  readonly csrfToken: string;
  readonly subject: string;
  readonly displayName?: string;
  readonly expiresAt: string;
}

let cachedSession: Promise<BrowserSessionView> | undefined;

async function boundedSessionText(response: Response): Promise<string> {
  const rawLength = response.headers.get("content-length");
  if (rawLength !== null) {
    if (!/^(?:0|[1-9][0-9]*)$/u.test(rawLength)) {
      throw new Error("The session response length was invalid.");
    }
    const declared = Number(rawLength);
    if (
      !Number.isSafeInteger(declared) ||
      declared > maximumSessionResponseCharacters
    ) {
      throw new Error("The session response exceeded its limit.");
    }
  }
  if (!response.body) return "";
  const reader = response.body.getReader();
  const chunks: Uint8Array[] = [];
  let total = 0;
  while (true) {
    const part = await reader.read();
    if (part.done) break;
    total += part.value.byteLength;
    if (total > maximumSessionResponseCharacters) {
      try {
        await reader.cancel();
      } catch {
        // The oversized response is rejected even if cancellation fails.
      }
      throw new Error("The session response exceeded its limit.");
    }
    chunks.push(part.value);
  }
  const bytes = new Uint8Array(total);
  let offset = 0;
  for (const chunk of chunks) {
    bytes.set(chunk, offset);
    offset += chunk.byteLength;
  }
  try {
    return new TextDecoder("utf-8", { fatal: true }).decode(bytes);
  } catch {
    throw new Error("The session response was invalid.");
  }
}

function isSession(value: unknown): value is BrowserSessionView {
  if (!value || typeof value !== "object" || Array.isArray(value)) return false;
  const record = value as Record<string, unknown>;
  return (
    record.authenticated === true &&
    typeof record.csrfToken === "string" &&
    csrfPattern.test(record.csrfToken) &&
    typeof record.subject === "string" &&
    record.subject.length > 0 &&
    record.subject.length <= 255 &&
    (record.displayName === undefined ||
      (typeof record.displayName === "string" &&
        record.displayName.length > 0 &&
        record.displayName.length <= 160)) &&
    typeof record.expiresAt === "string" &&
    Number.isFinite(Date.parse(record.expiresAt)) &&
    Date.parse(record.expiresAt) > Date.now()
  );
}

export async function browserSession(): Promise<BrowserSessionView> {
  cachedSession ??= (async () => {
    const response = await fetch("/auth/session", {
      cache: "no-store",
      credentials: "same-origin",
      headers: { Accept: "application/json" },
    });
    if (!response.ok) {
      throw new Error("A signed-in browser session is required.");
    }
    const text = await boundedSessionText(response);
    let value: unknown;
    try {
      value = JSON.parse(text);
    } catch {
      throw new Error("The session response was invalid.");
    }
    if (!isSession(value)) {
      throw new Error("A signed-in browser session is required.");
    }
    return value;
  })().catch((error: unknown) => {
    cachedSession = undefined;
    throw error;
  });
  return cachedSession;
}

export async function authenticatedFetch(
  input: RequestInfo | URL,
  init: RequestInit = {},
): Promise<Response> {
  if (typeof input !== "string" || /[\\\u0000-\u001F\u007F]/u.test(input)) {
    throw new TypeError(
      "Authenticated browser requests must be same-origin paths.",
    );
  }
  const origin = window.location.origin;
  const target = new URL(input, origin);
  if (
    target.origin !== origin ||
    target.hash ||
    (target.pathname !== "/auth/logout" &&
      !target.pathname.startsWith("/api/v1/"))
  ) {
    throw new TypeError(
      "Authenticated browser requests must be same-origin paths.",
    );
  }
  const destination = `${target.pathname}${target.search}`;
  const method = (init.method ?? "GET").toUpperCase();
  const headers = new Headers(init.headers);
  headers.delete("authorization");
  const session = await browserSession();
  if (method === "GET" || method === "HEAD" || method === "OPTIONS") {
    return fetch(destination, { ...init, credentials: "same-origin", headers });
  }
  headers.set(csrfHeaderName, session.csrfToken);
  return fetch(destination, {
    ...init,
    credentials: "same-origin",
    headers,
  });
}

export function clearBrowserSessionCache(): void {
  cachedSession = undefined;
}
