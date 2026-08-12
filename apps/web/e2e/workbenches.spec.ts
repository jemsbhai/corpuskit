import AxeBuilder from "@axe-core/playwright";
import { expect, type Page, test } from "@playwright/test";

const project = {
  id: "123e4567-e89b-42d3-a456-426614174001",
  name: "Acceptance project",
  description: "Cross-browser fixture",
  created_at: "2026-08-11T12:00:00Z",
};
const capabilityReport = {
  checked_at: "2026-08-11T12:00:00Z",
  ready: true,
  missing_required: [],
  checks: [
    {
      id: "corpusgen-core",
      state: "available",
      label: "CorpusGen engine",
      detail: "Ready.",
      remediation: null,
      version: "0.1.7",
      required: true,
    },
    {
      id: "espeak-g2p",
      state: "available",
      label: "eSpeak NG G2P",
      detail: "Ready.",
      remediation: null,
      version: null,
      required: false,
    },
    {
      id: "phoible",
      state: "available",
      label: "PHOIBLE inventory data",
      detail: "Ready.",
      remediation: null,
      version: "pinned",
      required: false,
    },
    {
      id: "optimization",
      state: "unavailable",
      label: "Optimization",
      detail: "Not installed.",
      remediation: "Install the reviewed optimization worker image.",
      version: null,
      required: false,
    },
  ],
};
const replaySourceRun = {
  id: "123e4567-e89b-42d3-a456-426614174080",
  project_id: project.id,
  corpus_version_id: null,
  parent_run_id: null,
  kind: "evaluate",
  state: "succeeded",
  attempt: 1,
  spec_sha256: "a".repeat(64),
  outbox_state: "published",
  cancellation_requested_at: null,
  created_at: "2026-08-11T12:00:00Z",
  result_summary: null,
  failure_code: null,
};
const queuedReplay = {
  replay_run_id: "123e4567-e89b-42d3-a456-426614174081",
  source_run_id: replaySourceRun.id,
  source_manifest_artifact_id: "123e4567-e89b-42d3-a456-426614174082",
  expected_manifest_sha256: "b".repeat(64),
  observed_manifest_artifact_id: null,
  classification: "exact",
  lifecycle: "queued",
  comparison: null,
};
const comparedReplay = {
  ...queuedReplay,
  observed_manifest_artifact_id: "123e4567-e89b-42d3-a456-426614174083",
  lifecycle: "compared",
  comparison: {
    classification: "exact",
    verdict: "exact-match",
    replay_inputs_match: true,
    outputs_match: true,
    differences: [],
  },
};

const routes = [
  ["/g2p", "G2P Studio"],
  ["/inventory", "Inventory Explorer"],
  ["/coverage", "Coverage & Weighting Lab"],
  ["/analysis", "Inventory & Analysis"],
  ["/selection", "Selection Studio"],
  ["/generation", "Generation & Scoring Studio"],
  ["/advanced", "Advanced Runtime Lab"],
  ["/jobs", "Job Center"],
  ["/artifacts", "Artifact Manager"],
  ["/capabilities", "Capability status"],
] as const;

async function mockApi(
  page: Page,
  options: {
    readonly replay?: boolean;
    readonly replayRequests?: Array<{
      readonly body: string | null;
      readonly idempotencyKey: string | undefined;
    }>;
  } = {},
) {
  await page.route("**/auth/session", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      headers: { "cache-control": "no-store" },
      body: JSON.stringify({
        authenticated: true,
        csrfToken: "t".repeat(43),
        subject: "user|editor",
        displayName: "Acceptance editor",
        expiresAt: "2099-01-01T00:00:00.000Z",
      }),
    }),
  );
  await page.route("**/api/v1/**", async (route) => {
    const url = new URL(route.request().url());
    let body: unknown = {};
    if (url.pathname === "/api/v1/auth/me")
      body = {
        subject: "user|editor",
        organization_id: "123e4567-e89b-42d3-a456-426614174000",
        role: "editor",
        display_name: "Acceptance editor",
      };
    else if (url.pathname === "/api/v1/projects") body = [project];
    else if (url.pathname.endsWith("/corpora")) body = [];
    else if (url.pathname === "/api/v1/capabilities") body = capabilityReport;
    else if (url.pathname === "/api/v1/platform/quota")
      body = {
        policy: {
          max_concurrent_cpu_jobs: 3,
          max_concurrent_expensive_jobs: 1,
          max_artifact_bytes: 10_000,
          max_artifact_count: 100,
          max_corpus_sentences: 1_000,
          max_generation_accepted_sentences: 100,
          max_generation_iterations: 500,
          max_activity_deadline_seconds: 300,
          max_provider_input_tokens: 1_000_000,
          max_provider_output_tokens: 100_000,
          max_provider_cost_microusd: 10_000_000,
          max_rl_steps: 10_000,
          max_rl_tokens: 10_000_000,
          max_checkpoint_bytes: 100_000_000,
        },
        usage: {
          active_cpu_jobs: 1,
          active_expensive_jobs: 0,
          artifact_bytes: 512,
          artifact_count: 2,
          corpus_sentences: 20,
        },
      };
    else if (url.pathname === "/api/v1/platform/audit-events")
      body = { events: [], next_cursor: null };
    else if (url.pathname === "/api/v1/labs/g2p/languages")
      body = { backend: "espeak", languages: ["en-us", "ar", "he"] };
    else if (url.pathname === "/api/v1/phonology/status")
      body = {
        cache_available: true,
        loaded: true,
        revision: "b".repeat(40),
        sha256: "c".repeat(64),
        stats: { inventory_count: 10, language_count: 5, segment_count: 100 },
      };
    else if (url.pathname === "/api/v1/phonology/features")
      body = {
        names: Array.from({ length: 38 }, (_, index) => `feature-${index + 1}`),
      };
    else if (
      options.replay &&
      url.pathname ===
        `/api/v1/projects/${project.id}/runs/${replaySourceRun.id}/replays` &&
      route.request().method() === "POST"
    ) {
      options.replayRequests?.push({
        body: route.request().postData(),
        idempotencyKey: route.request().headers()["idempotency-key"],
      });
      body = { replay: queuedReplay, created: true };
    } else if (
      options.replay &&
      url.pathname === `/api/v1/replays/${queuedReplay.replay_run_id}`
    )
      body = comparedReplay;
    else if (
      options.replay &&
      url.pathname === `/api/v1/runs/${replaySourceRun.id}/events`
    )
      body = [];
    else if (
      options.replay &&
      url.pathname === `/api/v1/runs/${replaySourceRun.id}`
    )
      body = replaySourceRun;
    else if (
      url.pathname === "/api/v1/runs" &&
      route.request().method() === "POST"
    )
      body = {
        id: "123e4567-e89b-42d3-a456-426614174099",
        project_id: project.id,
        corpus_version_id: null,
        parent_run_id: null,
        kind: "generate-llm",
        state: "queued",
        attempt: 0,
        spec_sha256: "a".repeat(64),
        outbox_state: "pending",
        cancellation_requested_at: null,
        created_at: "2026-08-11T12:00:00Z",
        result_summary: null,
        failure_code: null,
      };
    else if (url.pathname === "/api/v1/runs")
      body = options.replay ? [replaySourceRun] : [];
    else if (url.pathname === "/api/v1/advanced/capabilities")
      body = {
        schema_id: "corpuskit.advanced-capabilities.v2",
        advanced_operation_routes_validation_only: true,
        durable_run_submission_route: "/api/v1/runs",
        hosted_models: [
          {
            provider: "openai",
            model: "openai/demo-model",
            connection_id: "demo-provider",
            max_output_tokens_per_request: 128,
            request_delay_seconds: 0.25,
            prompt_template_ids: [],
          },
        ],
        huggingface_repositories: [
          {
            dataset: "acme/demo-corpus",
            config: "default",
            split: "train",
            text_column: "text",
            revision: "c".repeat(40),
            language: "en-us",
            max_samples: 100,
          },
        ],
        local_models: [],
        datg_runtimes: [],
        phon_rl_runtimes: [],
        datg_inspection: "unavailable",
        phon_rl_lab: "bounded_optional_dependency",
      };
    else if (url.pathname.endsWith("/datg/indexes")) body = [];
    else if (url.pathname.endsWith("/validate"))
      body = advancedValidationResponse(url.pathname);
    else if (url.pathname.endsWith("/estimate"))
      body = {
        schema_id: "corpuskit.hosted-cost-estimate.v1",
        estimated_ceiling_usd: "0.01",
        network_during_estimate: false,
      };
    else if (url.pathname === "/api/v1/labs/cli/preview")
      body = {
        workflow: "inventory",
        argv: ["corpusgen", "inventory"],
        posix_command: "PYTHONUTF8=1 corpusgen inventory eng --format json",
        powershell_command:
          "$env:PYTHONUTF8='1'; corpusgen inventory eng --format json",
        reproducibility: "external_dependency",
        warnings: [],
      };
    else if (url.pathname === "/api/v1/phon-rl/ppo/kl-penalty")
      body = { values: [[0, 1]] };
    else if (url.pathname === `/api/v1/projects/${project.id}/artifacts`)
      body = [];
    else body = [];
    await route.fulfill({
      contentType: "application/json",
      body: JSON.stringify(body),
    });
  });
}

function advancedValidationResponse(pathname: string): unknown {
  const operations: Readonly<Record<string, string>> = {
    "/api/v1/model-runtime/hosted/validate": "hosted_generation",
    "/api/v1/model-runtime/local/validate": "local_generation",
    "/api/v1/model-runtime/analysis/validate": "language_model_analysis",
    "/api/v1/datg/index/validate": "build_index",
    "/api/v1/datg/generation/validate": "guided_generation",
  };
  const operation = operations[pathname];
  const schemaId = pathname.startsWith("/api/v1/datg/")
    ? "corpuskit.datg-runtime-validation.v1"
    : pathname === "/api/v1/phon-rl/training/validate"
      ? "corpuskit.phon-rl-training-validation.v1"
      : "corpuskit.model-runtime-validation.v1";
  return {
    schema_id: schemaId,
    valid: true,
    ...(operation === undefined ? {} : { operation }),
    worker_only: true,
    network_during_validation: false,
  };
}

test("advanced workbench validates without inline execution and previews CLI safely", async ({
  page,
}) => {
  await mockApi(page);
  await page.goto("/advanced");
  await expect(
    page.getByRole("heading", { name: "Advanced Runtime Lab", level: 1 }),
  ).toBeVisible();
  await page
    .getByRole("checkbox", {
      name: /sends generation prompts and target details/i,
    })
    .check();
  await page.getByRole("button", { name: "Validate policy" }).click();
  await expect(page.getByText("Policy validation passed")).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Queue durable run" }),
  ).toBeEnabled();
  await page.getByRole("button", { name: "Queue durable run" }).click();
  await expect(page.getByText(/Hosted LLM generation queued/)).toBeVisible();
  await expect(
    page.getByRole("link", { name: "Open Job Center" }),
  ).toHaveAttribute("href", "/jobs");

  await page.getByLabel("Lab operation").selectOption("ppo-kl-penalty");
  await page.getByRole("button", { name: "Run bounded calculation" }).click();
  await expect(page.getByText("Lab result")).toBeVisible();

  await page.getByRole("button", { name: "Generate CLI preview" }).click();
  await expect(
    page.getByText(/PYTHONUTF8=1 corpusgen inventory/),
  ).toBeVisible();
  await expect(
    page.getByText(/quotes the preview but never invokes a shell/i),
  ).toBeVisible();
});

test("Job Center submits and compares a terminal run through the replay-only API", async ({
  page,
}) => {
  const replayRequests: Array<{
    readonly body: string | null;
    readonly idempotencyKey: string | undefined;
  }> = [];
  await mockApi(page, { replay: true, replayRequests });
  await page.goto("/jobs");

  const replay = page.getByRole("button", { name: "Replay this terminal run" });
  await expect(replay).toBeEnabled();
  await replay.click();
  await expect(
    page.getByRole("heading", { name: "Exact Match" }),
  ).toBeVisible();
  await expect(page.getByText("Replay-critical inputs")).toBeVisible();
  await expect(
    page.getByRole("link", { name: "Inspect source manifest" }),
  ).toHaveAttribute(
    "href",
    `/artifacts?artifact=${queuedReplay.source_manifest_artifact_id}`,
  );
  expect(replayRequests).toHaveLength(1);
  expect(replayRequests[0]?.body).toBeNull();
  expect(replayRequests[0]?.idempotencyKey).toMatch(
    new RegExp(`^ui-replay-${replaySourceRun.id}-[0-9a-f-]{36}$`, "u"),
  );
});

for (const [route, heading] of routes) {
  test(`${route} is accessible, keyboard reachable, and responsive at 200% text`, async ({
    page,
  }) => {
    const failures: string[] = [];
    page.on("console", (message) => {
      if (message.type() === "error")
        failures.push(`console: ${message.text()}`);
    });
    page.on("pageerror", (error) => failures.push(`page: ${error.message}`));
    page.on("requestfailed", (request) =>
      failures.push(`request: ${request.method()} ${request.url()}`),
    );
    await mockApi(page);
    await page.goto(route);
    await expect(
      page.getByRole("heading", { name: heading, level: 1 }),
    ).toBeVisible();
    // Wait for the footer's session bootstrap to settle before the page is eligible to
    // close. Otherwise a slow but valid request can be aborted by teardown and reported
    // as a product request failure under parallel browser load.
    await expect(page.getByText(/^Signed in as /u)).toBeVisible();
    const accessibility = await new AxeBuilder({ page })
      .withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa"])
      .analyze();
    expect(accessibility.violations).toEqual([]);

    await page.setViewportSize({ width: 320, height: 800 });
    const menuButton = page.getByRole("button", { name: "Toggle navigation" });
    await menuButton.focus();
    await expect(menuButton).toBeFocused();
    await page.keyboard.press("Enter");
    await expect(menuButton).toHaveAttribute("aria-expanded", "true");
    await page.evaluate(() => {
      document.documentElement.style.fontSize = "200%";
    });
    await expect(
      page.getByRole("heading", { name: heading, level: 1 }),
    ).toBeVisible();
    const layout = await page.evaluate(() => {
      const viewport = document.documentElement.clientWidth;
      const offenders = [...document.querySelectorAll("*")]
        .map((element) => {
          const rect = element.getBoundingClientRect();
          return {
            element: `${element.tagName.toLowerCase()}.${String(element.className).replaceAll(" ", ".")}`,
            right: Math.round(rect.right),
            scrollWidth: element.scrollWidth,
            width: Math.round(rect.width),
          };
        })
        .filter(
          (item) =>
            item.right > viewport + 1 || item.scrollWidth > item.width + 1,
        )
        .sort(
          (left, right) =>
            Math.max(right.right - viewport, right.scrollWidth - right.width) -
            Math.max(left.right - viewport, left.scrollWidth - left.width),
        )
        .slice(0, 20);
      return {
        overflow: document.documentElement.scrollWidth - viewport,
        viewport,
        mediaMatch: matchMedia("(max-width: 680px)").matches,
        rootFontSize: getComputedStyle(document.documentElement).fontSize,
        footerWhiteSpace: getComputedStyle(
          document.querySelector<HTMLElement>(".footer-status")!,
        ).whiteSpace,
        offenders,
      };
    });
    expect(layout.overflow, JSON.stringify(layout)).toBeLessThanOrEqual(1);
    expect(failures).toEqual([]);
  });
}
