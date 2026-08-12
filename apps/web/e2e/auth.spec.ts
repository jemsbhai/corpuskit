import { expect, test } from "@playwright/test";

test("every rendered page receives a fresh nonce that matches its scripts", async ({
  page,
}) => {
  const first = await page.goto("/");
  const firstPolicy = (await first?.headersArray())?.find(
    ({ name }) => name.toLowerCase() === "content-security-policy",
  )?.value;
  expect(firstPolicy).not.toContain("script-src 'self' 'unsafe-inline'");
  const firstNonce = /'nonce-([A-Za-z0-9+/]{48})'/u.exec(
    firstPolicy ?? "",
  )?.[1];
  expect(firstNonce).toBeTruthy();
  if (!firstNonce) throw new Error("Missing page CSP nonce.");
  const scriptNonces = await page
    .locator("script")
    .evaluateAll((scripts) => scripts.map((script) => script.nonce));
  const executableNonces = scriptNonces.filter(
    (nonce): nonce is string => typeof nonce === "string" && nonce.length > 0,
  );
  expect(executableNonces.length).toBeGreaterThan(0);
  expect(executableNonces.every((nonce) => nonce === firstNonce)).toBe(true);

  const second = await page.goto("/projects");
  const secondPolicy = (await second?.headersArray())?.find(
    ({ name }) => name.toLowerCase() === "content-security-policy",
  )?.value;
  const secondNonce = /'nonce-([A-Za-z0-9+/]{48})'/u.exec(
    secondPolicy ?? "",
  )?.[1];
  expect(secondNonce).toBeTruthy();
  expect(secondNonce).not.toBe(firstNonce);
});

test("local auth boundary issues a strict opaque cookie and enforces CSRF logout", async ({
  browserName,
  context,
  page,
}) => {
  test.skip(
    browserName === "webkit",
    "WebKit correctly declines Secure cookies on the HTTP-only local test server; production acceptance runs over TLS.",
  );
  let sessionResponse!: {
    status: number;
    headers: { name: string; value: string }[];
    session: {
      authenticated: boolean;
      csrfToken: string;
      subject: string;
      expiresAt: string;
    };
  };
  const sessionResponsePromise = page.waitForResponse(async (response) => {
    if (new URL(response.url()).pathname !== "/auth/session") return false;

    const status = response.status();
    const [session, headers] = await Promise.all([
      response.json() as Promise<{
        authenticated: boolean;
        csrfToken: string;
        subject: string;
        expiresAt: string;
      }>,
      response.headersArray(),
    ]);
    sessionResponse = { status, headers, session };
    return true;
  });
  await page.goto("/");
  await sessionResponsePromise;
  expect(sessionResponse.status).toBe(200);
  const setCookie = sessionResponse.headers
    .filter(({ name }) => name.toLowerCase() === "set-cookie")
    .map(({ value }) => value)
    .join("\n");
  expect(setCookie).toContain("SameSite=Lax");
  const { session } = sessionResponse;
  expect(session).toMatchObject({
    authenticated: true,
    subject: "deterministic-test-user",
  });
  expect(session.csrfToken).toMatch(/^[A-Za-z0-9_-]{43}$/u);
  expect(Date.parse(session.expiresAt)).toBeGreaterThan(Date.now());
  expect(JSON.stringify(session)).not.toMatch(/access|refresh|id.?token/iu);

  const cookies = await context.cookies();
  const cookie = cookies.find(
    ({ name }) => name === "__Host-corpuskit_session",
  );
  expect(cookie).toMatchObject({
    httpOnly: true,
    secure: true,
    path: "/",
  });
  expect(cookie?.value).toMatch(/^[A-Za-z0-9_-]{43}$/u);

  const denied = await page.evaluate(async () => {
    const response = await fetch("/auth/logout", {
      method: "POST",
      credentials: "same-origin",
    });
    return response.status;
  });
  expect(denied).toBe(403);

  const completed = await page.evaluate(async (csrfToken) => {
    const response = await fetch("/auth/logout", {
      method: "POST",
      credentials: "same-origin",
      headers: { "x-corpuskit-csrf": csrfToken },
    });
    return response.status;
  }, session.csrfToken);
  expect(completed).toBe(204);
  expect(
    (await context.cookies()).some(
      ({ name }) => name === "__Host-corpuskit_session",
    ),
  ).toBe(false);
});
