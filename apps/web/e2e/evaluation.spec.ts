import AxeBuilder from "@axe-core/playwright";
import { expect, test } from "@playwright/test";

const evaluationResponse = {
  language: "en-us",
  unit: "phoneme",
  target_mode: "explicit",
  target_units: ["h", "ə", "l", "oʊ", "z"],
  covered_units: ["h", "ə", "l", "oʊ"],
  missing_units: ["z"],
  coverage: 0.8,
  total_sentences: 1,
  unit_counts: [{ unit: "h", count: 1 }],
  sentence_details: [
    {
      index: 0,
      text: "Hello.",
      phoneme_count: 4,
      new_units: ["h", "ə", "l", "oʊ"],
      all_phonemes: ["h", "ə", "l", "oʊ"],
    },
  ],
  unit_sources: [{ unit: "h", sentence_indices: [0] }],
  distribution: {
    entropy: 2,
    normalized_entropy: 0.86,
    jsd_uniform: 0.08,
    coefficient_of_variation: 0.3,
    min_count: 0,
    max_count: 1,
    count_ratio: 0,
    zero_count: 1,
    pcd_uniform: 0.736,
    jsd_reference: null,
    pearson_correlation: null,
  },
  text_quality: {
    sentence_length_words_mean: 1,
    sentence_length_words_median: 1,
    sentence_length_words_std: 0,
    sentence_length_words_min: 1,
    sentence_length_words_max: 1,
    sentence_length_phonemes_mean: 4,
    sentence_length_phonemes_median: 4,
    sentence_length_phonemes_std: 0,
    sentence_length_phonemes_min: 4,
    sentence_length_phonemes_max: 4,
    total_words: 1,
    unique_words: 1,
    type_token_ratio: 1,
    hapax_ratio: 1,
    flesch_reading_ease: 121.2,
    flesch_kincaid_grade: -3.4,
  },
};

test("evaluation workflow is keyboard-accessible and has no detectable axe violations", async ({
  page,
}) => {
  await page.route("**/api/v1/evaluations", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(evaluationResponse),
    });
  });
  await page.goto("/evaluate");

  await expect(
    page.getByRole("heading", { name: "Evaluation Studio" }),
  ).toBeVisible();
  const sentenceEditor = page.getByRole("textbox", { name: /sentences/i });
  await sentenceEditor.pressSequentially("Hello.");
  await expect(sentenceEditor).toHaveValue("Hello.");
  await page.getByRole("radio", { name: /explicit IPA list/i }).check();
  await page
    .getByRole("textbox", { name: /target phonemes/i })
    .fill("h, ə, l, oʊ, z");
  await page.getByRole("button", { name: "Evaluate corpus" }).click();

  await expect(
    page.getByRole("heading", { name: "80.0% coverage" }),
  ).toBeVisible();
  await expect(page.getByRole("table")).toContainText("Hello.");
  const accessibility = await new AxeBuilder({ page }).analyze();
  expect(accessibility.violations).toEqual([]);
});

test("empty evaluation studio has no detectable axe violations", async ({
  page,
}) => {
  await page.goto("/evaluate");
  await expect(
    page.getByRole("heading", { name: "Evaluation Studio" }),
  ).toBeVisible();
  const accessibility = await new AxeBuilder({ page }).analyze();
  expect(accessibility.violations).toEqual([]);
});
