import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("@/lib/browser-auth", () => ({ authenticatedFetch: vi.fn() }));

import { authenticatedFetch } from "@/lib/browser-auth";
import { platformApi } from "@/lib/platform";

const fetchMock = vi.mocked(authenticatedFetch);
const json = (value: unknown) =>
  new Response(JSON.stringify(value), {
    headers: { "content-type": "application/json" },
  });

const quota = {
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

const event = {
  sequence: 7,
  actor_kind: "user",
  actor_id: "user|demo",
  action: "corpus.version_created",
  resource_type: "corpus",
  resource_id: "123e4567-e89b-42d3-a456-426614174009",
  request_id: "request-7",
  occurred_at: "2026-08-11T12:00:00Z",
  metadata: { version_number: 2 },
  previous_hash: "a".repeat(64),
  event_hash: "b".repeat(64),
};

afterEach(() => fetchMock.mockReset());

describe("platform governance API", () => {
  it("loads strict quota and cursor-paginated audit contracts", async () => {
    fetchMock
      .mockResolvedValueOnce(json(quota))
      .mockResolvedValueOnce(json({ events: [event], next_cursor: "6" }));

    await expect(platformApi.quota()).resolves.toEqual(quota);
    await expect(
      platformApi.auditEvents({ cursor: "7", limit: 25 }),
    ).resolves.toEqual({ events: [event], next_cursor: "6" });
    expect(fetchMock.mock.calls.map(([path]) => path)).toEqual([
      "/api/v1/platform/quota",
      "/api/v1/platform/audit-events?cursor=7&limit=25",
    ]);
  });

  it.each([
    { ...quota, usage: { ...quota.usage, artifact_count: -1 } },
    {
      ...quota,
      policy: { ...quota.policy, max_activity_deadline_seconds: Infinity },
    },
  ])("rejects malformed quota variant %#", async (value) => {
    fetchMock.mockResolvedValueOnce(json(value));
    await expect(platformApi.quota()).rejects.toThrow("incompatible response");
  });

  it.each([
    { ...event, action: "raw.secret.read" },
    { ...event, event_hash: "short" },
    { ...event, occurred_at: "today" },
  ])("rejects malformed audit event variant %#", async (value) => {
    fetchMock.mockResolvedValueOnce(
      json({ events: [value], next_cursor: null }),
    );
    await expect(platformApi.auditEvents()).rejects.toThrow(
      "incompatible response",
    );
  });

  it("rejects client-side audit page sizes outside the API bounds", () => {
    expect(() => platformApi.auditEvents({ limit: 0 })).toThrow(TypeError);
    expect(() => platformApi.auditEvents({ limit: 201 })).toThrow(TypeError);
    expect(fetchMock).not.toHaveBeenCalled();
  });
});
