import AxeBuilder from "@axe-core/playwright";
import { expect, test } from "@playwright/test";

const inventory = {
  inventory_id: 1,
  language_name: "English",
  iso639_3: "eng",
  glottocode: "stan1293",
  specific_dialect: null,
  source: "phoible",
  segments: [
    {
      phoneme: "p",
      segment_class: "consonant",
      marginal: false,
      allophones: ["pʰ"],
      features: [
        { name: "voice", value: "-" },
        { name: "contour", value: "0,0,-,+" },
      ],
      glyph_id: "1",
    },
  ],
  phonemes: ["p"],
  consonants: ["p"],
  vowels: [],
  tones: [],
  marginal_phonemes: [],
  size: 1,
  consonant_count: 1,
  vowel_count: 0,
  tone_count: 0,
};

test("inventory search and segment browsing are accessible", async ({
  page,
}) => {
  await page.route("**/api/v1/phonology/status", (route) =>
    route.fulfill({
      contentType: "application/json",
      body: JSON.stringify({
        cache_available: true,
        loaded: true,
        revision: "b".repeat(40),
        sha256: "c".repeat(64),
        stats: { inventory_count: 10, language_count: 5, segment_count: 100 },
      }),
    }),
  );
  await page.route("**/api/v1/phonology/languages**", (route) =>
    route.fulfill({
      contentType: "application/json",
      body: JSON.stringify({
        items: [
          {
            iso639_3: "eng",
            glottocode: "stan1293",
            language_name: "English",
            inventory_count: 1,
            sources: ["phoible"],
          },
        ],
        total: 1,
        offset: 0,
        limit: 50,
      }),
    }),
  );
  await page.route("**/api/v1/phonology/espeak-mappings**", (route) =>
    route.fulfill({
      contentType: "application/json",
      body: JSON.stringify({
        items: [{ espeak_code: "en-us", iso639_3: "eng" }],
        total: 1,
        offset: 0,
        limit: 50,
      }),
    }),
  );
  await page.route("**/api/v1/phonology/inventories/eng**", (route) =>
    route.fulfill({
      contentType: "application/json",
      body: JSON.stringify(inventory),
    }),
  );
  await page.goto("/analysis");
  await expect(
    page.getByRole("heading", { name: "Inventory & Analysis" }),
  ).toBeVisible();
  await page
    .getByRole("textbox", { name: "Language name or code" })
    .fill("English");
  await page.getByRole("button", { name: "Search languages" }).click();
  await page.getByRole("button", { name: "Browse" }).first().click();
  await expect(
    page.getByRole("heading", { name: "English", exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("table", { name: /segments in the selected/i }),
  ).toContainText("p");
  const accessibility = await new AxeBuilder({ page }).analyze();
  expect(accessibility.violations).toEqual([]);
});

test("no-cache inventory state is explicit and accessible", async ({
  page,
}) => {
  await page.route("**/api/v1/phonology/status", (route) =>
    route.fulfill({
      contentType: "application/json",
      body: JSON.stringify({
        cache_available: false,
        loaded: false,
        revision: "b".repeat(40),
        sha256: "c".repeat(64),
        stats: null,
      }),
    }),
  );
  await page.goto("/analysis");
  await expect(
    page.getByRole("heading", { name: "PHOIBLE data is not provisioned" }),
  ).toBeVisible();
  const accessibility = await new AxeBuilder({ page }).analyze();
  expect(accessibility.violations).toEqual([]);
});

test("coverage trajectory returns an exact live-analysis table", async ({
  page,
}) => {
  await page.route("**/api/v1/phonology/status", (route) =>
    route.fulfill({
      contentType: "application/json",
      body: JSON.stringify({
        cache_available: true,
        loaded: true,
        revision: "b".repeat(40),
        sha256: "c".repeat(64),
        stats: null,
      }),
    }),
  );
  await page.route("**/api/v1/analyses/coverage-trajectory", (route) =>
    route.fulfill({
      contentType: "application/json",
      body: JSON.stringify({
        unit: "phoneme",
        target_size: 2,
        coverages: [0.5, 1],
        gains: [1, 1],
        snapshots: [
          {
            sentence_index: 0,
            coverage: 0.5,
            covered_count: 1,
            new_units_count: 1,
            new_units: ["p"],
          },
          {
            sentence_index: 1,
            coverage: 1,
            covered_count: 2,
            new_units_count: 1,
            new_units: ["b"],
          },
        ],
      }),
    }),
  );
  await page.goto("/analysis");
  await page.getByRole("tab", { name: /coverage trajectory/i }).click();
  await page
    .getByRole("textbox", { name: /phoneme sequences/i })
    .fill("p\np b");
  await page.getByRole("textbox", { name: /target units/i }).fill("p b");
  await page.getByRole("button", { name: "Build trajectory" }).click();
  await expect(
    page.getByRole("heading", { name: "100.0% final coverage" }),
  ).toBeVisible();
  await expect(
    page.getByRole("table", { name: "Coverage after each sentence" }),
  ).toContainText("50.0%");
  const accessibility = await new AxeBuilder({ page }).analyze();
  expect(accessibility.violations).toEqual([]);
});
