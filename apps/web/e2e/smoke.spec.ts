import AxeBuilder from "@axe-core/playwright";
import { expect, test } from "@playwright/test";

test("guided demo dashboard and capability fallback remain usable", async ({
  page,
}) => {
  await page.goto("/");

  await expect(
    page.getByRole("heading", { name: /design speech corpora with evidence/i }),
  ).toBeVisible();
  await expect(
    page.getByText("Illustrative walkthrough", { exact: true }),
  ).toBeVisible();

  await page.getByRole("link", { name: "Capability status" }).first().click();
  await expect(page).toHaveURL(/\/capabilities$/);
  await expect(
    page.getByRole("heading", { name: "Capability status" }),
  ).toBeVisible();
  await expect(
    page.getByText(/preview data|live service status/i),
  ).toBeVisible();
});

for (const route of ["/", "/capabilities"] as const) {
  test(`${route} has no WCAG 2.2 A/AA violations`, async ({ page }) => {
    await page.goto(route);
    await expect(
      page.getByRole("heading", {
        name:
          route === "/"
            ? /design speech corpora with evidence/i
            : "Capability status",
      }),
    ).toBeVisible();
    const results = await new AxeBuilder({ page })
      .withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa"])
      .analyze();

    expect(results.violations).toEqual([]);
  });
}

test("the full navigation collapses before laptop-width overflow", async ({
  page,
}) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto("/");

  const menu = page.getByRole("button", { name: "Toggle navigation" });
  await expect(menu).toBeVisible();
  const layout = await page.evaluate(() => ({
    clientWidth: document.documentElement.clientWidth,
    scrollWidth: document.documentElement.scrollWidth,
  }));
  expect(layout.scrollWidth - layout.clientWidth).toBeLessThanOrEqual(1);

  await menu.click();
  await expect(menu).toHaveAttribute("aria-expanded", "true");
  await expect(
    page.getByRole("navigation", { name: "Primary navigation" }),
  ).toBeVisible();
});
