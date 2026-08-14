import { expect, test } from "@playwright/test";

test("real stack creates a corpus, appends a version, and preserves history", async ({
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
  await page.getByLabel("Version eSpeak language").fill("en-gb");
  await page
    .getByLabel("Version sentences")
    .fill("Revised hello world\nA second immutable sentence");
  await page.getByRole("button", { name: "Create version" }).click();
  await expect(page.getByRole("button", { name: /Version 2/ })).toHaveAttribute(
    "aria-pressed",
    "true",
  );
  await expect(
    page.getByRole("table", { name: /Normalized sentences/ }),
  ).toContainText("A second immutable sentence");

  await page.getByRole("button", { name: /Version 1/ }).click();
  await expect(
    page.getByRole("table", { name: /Normalized sentences/ }),
  ).toContainText("你好世界");
  const downloadPromise = page.waitForEvent("download");
  await page.getByRole("link", { name: "JSON" }).click();
  const download = await downloadPromise;
  expect(download.suggestedFilename()).toMatch(/-v1\.json$/u);
});
