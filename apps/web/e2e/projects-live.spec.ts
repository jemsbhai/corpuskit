import { expect, test } from "@playwright/test";

test.skip(
  !process.env.CORPUSKIT_LIVE_STACK,
  "set CORPUSKIT_LIVE_STACK=1 for real API acceptance",
);

test("real stack creates a tenant project and immutable manual corpus", async ({
  page,
}) => {
  const suffix = `${Date.now()}-${test.info().project.name}`;
  const projectName = `Live workspace ${suffix}`;
  const corpusName = `Live corpus ${suffix}`;
  await page.goto("/projects");
  await page.getByLabel("Project name").fill(projectName);
  await page.getByLabel("Description").fill("Playwright real-stack acceptance");
  await page.getByRole("button", { name: "Create project" }).click();
  await expect(
    page.getByRole("button", { name: new RegExp(projectName, "u") }),
  ).toBeVisible();
  await page.getByLabel("Corpus name").fill(corpusName);
  await page.getByLabel("Sentences one per line").fill("Héllo world\n你好世界");
  await page.getByRole("button", { name: "Create corpus" }).click();
  await expect(
    page.getByRole("table", { name: /Normalized sentences/ }),
  ).toContainText("你好世界");
  const downloadPromise = page.waitForEvent("download");
  await page.getByRole("link", { name: "JSON" }).click();
  const download = await downloadPromise;
  expect(download.suggestedFilename()).toMatch(/-v1\.json$/u);
});
