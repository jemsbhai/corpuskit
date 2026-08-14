import AxeBuilder from "@axe-core/playwright";
import { expect, test } from "@playwright/test";

const project = {
  id: "00000000-0000-4000-8000-000000000101",
  name: "Demo project",
  description: "Acceptance workspace",
  created_at: "2026-08-11T00:00:00Z",
};
const principal = {
  subject: "oidc|playwright-owner",
  organization_id: "00000000-0000-4000-8000-000000000001",
  role: "owner",
  display_name: "Playwright Owner",
};
const corpus = {
  id: "00000000-0000-4000-8000-000000000102",
  project_id: project.id,
  name: "Unicode seed",
  created_at: "2026-08-11T00:00:00Z",
};
const version = {
  id: "00000000-0000-4000-8000-000000000103",
  corpus_id: corpus.id,
  parent_version_id: null,
  version_number: 1,
  language: "en-us",
  sentence_count: 2,
  content_sha256: "a".repeat(64),
  corpusgen_version: "0.1.7",
  created_at: "2026-08-11T00:00:00Z",
};
const versionTwo = {
  ...version,
  id: "00000000-0000-4000-8000-000000000104",
  parent_version_id: version.id,
  version_number: 2,
  language: "en-gb",
  content_sha256: "b".repeat(64),
};

test("manual corpus workflow is keyboard-accessible and exports are downloadable", async ({
  page,
}) => {
  let corpusCreated = false;
  await page.route("**/api/v1/**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname;
    if (path === "/api/v1/auth/me" && request.method() === "GET") {
      await route.fulfill({ json: principal });
    } else if (path === "/api/v1/projects" && request.method() === "GET") {
      await route.fulfill({ json: [project] });
    } else if (
      path.endsWith(`/projects/${project.id}/corpora`) &&
      request.method() === "GET"
    ) {
      await route.fulfill({ json: corpusCreated ? [corpus] : [] });
    } else if (
      path.endsWith(`/projects/${project.id}/corpora`) &&
      request.method() === "POST"
    ) {
      corpusCreated = true;
      await route.fulfill({ status: 201, json: { corpus, version } });
    } else if (path.endsWith(`/corpora/${corpus.id}/versions`)) {
      await route.fulfill({ json: [version] });
    } else if (path.endsWith(`/versions/${version.id}/sentences`)) {
      await route.fulfill({
        json: [
          { ordinal: 0, original_text: "  Héllo  ", normalized_text: "Héllo" },
          { ordinal: 1, original_text: "你好", normalized_text: "你好" },
        ],
      });
    } else if (path.endsWith(`/versions/${version.id}/export`)) {
      await route.fulfill({
        body: "Héllo\n你好\n",
        contentType: "text/plain; charset=utf-8",
        headers: {
          "content-disposition":
            "attachment; filename=\"unicode-seed-v1.txt\"; filename*=UTF-8''unicode-seed-v1.txt",
        },
      });
    } else {
      await route.fulfill({
        status: 404,
        json: { code: "not_found", message: "Not found." },
      });
    }
  });

  await page.goto("/projects");
  await expect(
    page.getByRole("heading", { name: "Project workspaces" }),
  ).toBeVisible();
  await page.getByRole("button", { name: /Demo project/ }).click();
  await page.getByLabel("Corpus name").fill(corpus.name);
  await page
    .getByLabel("Sentences one per line")
    .fill("  Héllo  \n\nHéllo\n你好");
  await page.getByRole("button", { name: "Create corpus" }).click();
  await expect(page.getByText(version.content_sha256)).toBeVisible();
  await expect(
    page.getByRole("table", { name: /Normalized sentences/ }),
  ).toContainText("你好");

  const downloadPromise = page.waitForEvent("download");
  await page.getByRole("link", { name: "TXT" }).click();
  const download = await downloadPromise;
  // Browser engines differ on whether a same-origin `download` value or the
  // stricter server Content-Disposition wins; both names are bounded and safe.
  expect(download.suggestedFilename()).toMatch(
    /^(?:corpus-v1|unicode-seed-v1)\.txt$/u,
  );

  const accessibility = await new AxeBuilder({ page }).analyze();
  expect(accessibility.violations).toEqual([]);
});

test("CSV import requires an explicit text column", async ({ page }) => {
  let uploadBody = "";
  await page.route("**/api/v1/**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (path === "/api/v1/auth/me" && request.method() === "GET") {
      await route.fulfill({ json: principal });
    } else if (path === "/api/v1/projects") {
      await route.fulfill({ json: [project] });
    } else if (path.endsWith(`/projects/${project.id}/corpora/imports`)) {
      uploadBody = request.postData() ?? "";
      await route.fulfill({ status: 201, json: { corpus, version } });
    } else if (path.endsWith(`/projects/${project.id}/corpora`)) {
      await route.fulfill({ json: uploadBody ? [corpus] : [] });
    } else if (path.endsWith(`/corpora/${corpus.id}/versions`)) {
      await route.fulfill({ json: [version] });
    } else if (path.endsWith(`/versions/${version.id}/sentences`)) {
      await route.fulfill({ json: [] });
    } else {
      await route.fulfill({ status: 404 });
    }
  });

  await page.goto("/projects");
  await page.getByRole("button", { name: /Demo project/ }).click();
  await page.getByRole("radio", { name: "File import" }).check();
  await page.getByLabel("Corpus name").fill(corpus.name);
  await page.getByLabel("File format").selectOption("csv");
  await page.getByLabel("CSV text column").fill("utterance");
  await page.getByLabel("UTF-8 CSV file").setInputFiles({
    name: "seed.csv",
    mimeType: "text/csv",
    buffer: Buffer.from("utterance\nHéllo\n", "utf-8"),
  });
  await page.getByRole("button", { name: "Create corpus" }).click();
  await expect.poll(() => uploadBody).toContain("utterance");
  expect(uploadBody).toContain("seed.csv");
  expect(uploadBody).toContain("text/csv");
});

test("manual corpus version creation refreshes and selects immutable history", async ({
  page,
}) => {
  let submitted: unknown = null;
  let appended = false;
  await page.route("**/api/v1/**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (path === "/api/v1/auth/me" && request.method() === "GET") {
      await route.fulfill({ json: principal });
    } else if (path === "/api/v1/projects" && request.method() === "GET") {
      await route.fulfill({ json: [project] });
    } else if (
      path.endsWith(`/projects/${project.id}/corpora`) &&
      request.method() === "GET"
    ) {
      await route.fulfill({ json: [corpus] });
    } else if (
      path.endsWith(`/corpora/${corpus.id}/versions`) &&
      request.method() === "POST"
    ) {
      submitted = request.postDataJSON();
      appended = true;
      await route.fulfill({ status: 201, json: versionTwo });
    } else if (
      path.endsWith(`/corpora/${corpus.id}/versions`) &&
      request.method() === "GET"
    ) {
      await route.fulfill({
        json: appended ? [version, versionTwo] : [version],
      });
    } else if (path.endsWith(`/versions/${version.id}/sentences`)) {
      await route.fulfill({
        json: [
          { ordinal: 0, original_text: "First", normalized_text: "First" },
        ],
      });
    } else if (path.endsWith(`/versions/${versionTwo.id}/sentences`)) {
      await route.fulfill({
        json: [
          {
            ordinal: 0,
            original_text: " Revised ",
            normalized_text: "Revised",
          },
        ],
      });
    } else {
      await route.fulfill({ status: 404 });
    }
  });

  await page.goto("/projects");
  await page.getByRole("button", { name: /Demo project/ }).click();
  await page.getByRole("button", { name: /Unicode seed/ }).click();
  await page.getByLabel("Version eSpeak language").fill("en-gb");
  await page.getByLabel("Version sentences").fill(" Revised ");
  await page.getByRole("button", { name: "Create version" }).click();

  await expect
    .poll(() => submitted)
    .toEqual({
      language: "en-gb",
      sentences: [" Revised "],
    });
  await expect(page.getByRole("button", { name: /Version 2/ })).toHaveAttribute(
    "aria-pressed",
    "true",
  );
  await expect(
    page.getByRole("table", { name: /Normalized sentences/ }),
  ).toContainText("Revised");
  const accessibility = await new AxeBuilder({ page }).analyze();
  expect(accessibility.violations).toEqual([]);
});
