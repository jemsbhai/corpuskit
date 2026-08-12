import { readFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";

import AxeBuilder from "@axe-core/playwright";
import { expect, test } from "@playwright/test";

const fixturePath = fileURLToPath(
  new URL("./fixtures/demo-corpus.txt", import.meta.url),
);

test("15-minute demo exercises the real stack without request mocks", async ({
  page,
}) => {
  const fixture = (await readFile(fixturePath, "utf8")).trim();
  const runId = `${Date.now()}-${process.pid}`;
  const projectName = `CorpusKit demo ${runId}`;
  const corpusName = `Fixed pangram corpus ${runId}`;
  const failedResponses: string[] = [];
  page.on("response", (response) => {
    if (response.url().includes("/api/v1/") && response.status() >= 500)
      failedResponses.push(`${response.status()} ${response.url()}`);
  });

  await test.step("create one project and immutable fixed corpus", async () => {
    await page.goto("/projects");
    await page.getByLabel("Project name").fill(projectName);
    await page
      .getByLabel("Description")
      .fill("No-mock 15-minute acceptance fixture");
    await page.getByRole("button", { name: "Create project" }).click();
    await expect(page.getByLabel("Active project")).toHaveValue(/.+/u);
    await expect(page.getByLabel("Active project")).toContainText(projectName);

    await page.getByLabel("Corpus name").fill(corpusName);
    await page.getByLabel("Sentences one per line").fill(fixture);
    await page.getByRole("button", { name: "Create corpus" }).click();
    await expect(
      page.getByRole("table", { name: /Normalized sentences/iu }),
    ).toContainText("Pack my box");
    await expect(page.getByText(/SHA-256/iu).first()).toBeVisible();
  });

  await test.step("preserve global project and evaluate live text", async () => {
    await page.goto("/evaluate");
    await expect(page.getByLabel("Active project")).toContainText(projectName);
    await page.getByLabel(/Sentences one per line/iu).fill(fixture);
    await page.getByRole("button", { name: "Evaluate corpus" }).click();
    await expect(page.getByText("Live API result")).toBeVisible({
      timeout: 30_000,
    });
    await expect(page.getByRole("table").first()).toContainText("Pack my box");
  });

  await test.step("compare available selection algorithms on identical rows", async () => {
    await page.goto("/selection");
    await page.getByLabel("Candidate sentences · one per line").fill(fixture);
    const algorithms = ["greedy", "celf", "stochastic", "distribution"];
    for (const algorithm of algorithms) {
      await page.getByLabel("Algorithm").selectOption(algorithm);
      await page.getByRole("button", { name: "Run selection" }).click();
      await expect(
        page.getByRole("heading", {
          level: 3,
          name: new RegExp(`^${algorithm}`, "iu"),
        }),
      ).toBeVisible({ timeout: 30_000 });
    }
    const optional = ["ilp", "nsga2"];
    for (const algorithm of optional) {
      const option = page.getByRole("option", {
        name: algorithm === "nsga2" ? "NSGA-II" : "ILP",
      });
      if (await option.isEnabled()) {
        await page.getByLabel("Algorithm").selectOption(algorithm);
        await page.getByRole("button", { name: "Run selection" }).click();
        await expect(
          page.getByRole("heading", {
            level: 3,
            name: new RegExp(`^${algorithm}`, "iu"),
          }),
        ).toBeVisible({ timeout: 30_000 });
      }
    }
    await expect(
      page.getByRole("region", { name: "Selection comparison" }),
    ).toBeVisible();
  });

  await test.step("exercise explicit scoring controls and live results", async () => {
    await page.goto("/generation");
    await page.getByRole("tab", { name: "Composite scoring" }).click();
    await page.getByLabel("Coverage weight").fill("2");
    await page.getByLabel("Readability weight").fill("1");
    await page.getByLabel("Readability target minimum").fill("40");
    await page.getByLabel("Readability target maximum").fill("85");
    await page.getByRole("button", { name: "Rank candidates" }).click();
    await expect(
      page.getByRole("region", { name: "Composite scoring results" }),
    ).toBeVisible({ timeout: 30_000 });
  });

  await test.step("queue a real durable CPU run and inspect its lifecycle", async () => {
    await page.goto("/jobs");
    await expect(
      page.getByText(`Project: ${projectName}`).first(),
    ).toBeVisible();
    await page.getByRole("button", { name: "Submit durable run" }).click();
    const runList = page.getByRole("list", {
      name: "Runs in selected project",
    });
    await expect(runList).toBeVisible({ timeout: 30_000 });
    await page.getByRole("button", { name: "Refresh" }).click();
    await expect(runList.getByRole("button").first()).toContainText(
      /queued|provisioning|running|succeeded/iu,
    );
  });

  await test.step("show provenance, capability refresh, quota, and audit", async () => {
    await page.goto("/inventory");
    await expect(page.getByText(/Snapshot/iu)).toBeVisible();
    await expect(page.getByText(/SHA-256/iu).first()).toBeVisible();

    await page.goto("/capabilities");
    await page
      .getByRole("button", { name: "Refresh capability checks" })
      .click();
    await expect(
      page.getByRole("heading", { name: "Current quota usage" }),
    ).toBeVisible();
    await expect(
      page.getByRole("heading", { name: "Tenant audit events" }),
    ).toBeVisible();
  });

  const accessibility = await new AxeBuilder({ page })
    .withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa"])
    .analyze();
  expect(accessibility.violations).toEqual([]);
  expect(failedResponses).toEqual([]);
});
