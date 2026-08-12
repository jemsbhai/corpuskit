import { expect, test } from "@playwright/test";

test.describe("real API vertical acceptance", () => {
  test.skip(
    process.env.CORPUSKIT_LIVE_STACK !== "1",
    "Set CORPUSKIT_LIVE_STACK=1 with real API and web containers to run.",
  );

  test("submits a derived English evaluation through the same-origin BFF", async ({
    page,
  }) => {
    await page.goto("/evaluate");
    await page
      .getByRole("textbox", { name: /sentences/i })
      .fill("The quick brown fox jumps over the lazy dog.");
    await page.getByRole("button", { name: "Evaluate corpus" }).click();

    await expect(page.getByText("Live API result")).toBeVisible({
      timeout: 30_000,
    });
    await expect(
      page.getByRole("heading", { name: /% coverage$/i }),
    ).toBeVisible();
    await expect(page.getByRole("table")).toContainText("The quick brown fox");
  });
});
