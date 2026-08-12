import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("@/lib/browser-auth", () => ({ authenticatedFetch: vi.fn() }));

import {
  buildSupportedRunSpec,
  isTerminalRun,
  jobsApi,
  mergeMonotonicEvents,
  nextEventCursor,
  parseEvents,
  parseRunProgress,
  type JobBuilderInput,
  type RunEvent,
} from "@/lib/jobs";
import { authenticatedFetch } from "@/lib/browser-auth";

const fetchMock = vi.mocked(authenticatedFetch);
afterEach(() => fetchMock.mockReset());

const base: JobBuilderInput = {
  language: "en-us",
  unit: "phoneme",
  primaryRows: ["one", "two"],
  secondaryRows: ["won", "too"],
  targetUnits: ["p", "b"],
  counts: [{ unit: "p", count: 2 }],
  caseSensitive: false,
  seed: 42,
};

describe("typed durable run builders", () => {
  it("builds only the six registered core specifications", () => {
    expect(buildSupportedRunSpec("phonemize", base)).toEqual({
      texts: ["one", "two"],
      language: "en-us",
    });
    expect(buildSupportedRunSpec("evaluate", base)).toMatchObject({
      sentences: ["one", "two"],
      unit: "phoneme",
      target: { mode: "explicit", phonemes: ["p", "b"] },
    });
    expect(buildSupportedRunSpec("distribution", base)).toEqual({
      counts: [{ unit: "p", count: 2 }],
      target_units: ["p", "b"],
      reference_distribution: null,
    });
    expect(
      buildSupportedRunSpec("trajectory", {
        ...base,
        primaryRows: ["p a", "b a"],
      }),
    ).toEqual({
      phoneme_sequences: [
        ["p", "a"],
        ["b", "a"],
      ],
      target_units: ["p", "b"],
      unit: "phoneme",
    });
    expect(
      buildSupportedRunSpec("error-rates", { ...base, caseSensitive: true }),
    ).toMatchObject({
      references: ["one", "two"],
      hypotheses: ["won", "too"],
      case_sensitive: true,
      reference_phonemes: null,
    });
    expect(buildSupportedRunSpec("select", base)).toMatchObject({
      candidates: ["one", "two"],
      options: { algorithm: "greedy", seed: 42, max_sentences: 2 },
    });
  });

  it("uses a derived target when no explicit units were provided", () => {
    expect(
      buildSupportedRunSpec("evaluate", { ...base, targetUnits: [] }),
    ).toMatchObject({ target: { mode: "derived", phonemes: [] } });
  });

  it("rejects unsafe, empty, unpaired, and unreplayable builder inputs", () => {
    expect(() =>
      buildSupportedRunSpec("evaluate", { ...base, language: "" }),
    ).toThrow(TypeError);
    expect(() =>
      buildSupportedRunSpec("evaluate", { ...base, language: "x".repeat(33) }),
    ).toThrow(TypeError);
    expect(() =>
      buildSupportedRunSpec("evaluate", { ...base, primaryRows: [] }),
    ).toThrow(TypeError);
    expect(() =>
      buildSupportedRunSpec("trajectory", { ...base, primaryRows: ["  "] }),
    ).toThrow(TypeError);
    expect(() =>
      buildSupportedRunSpec("error-rates", { ...base, secondaryRows: ["one"] }),
    ).toThrow(TypeError);
    expect(() =>
      buildSupportedRunSpec("select", { ...base, seed: -1 }),
    ).toThrow(TypeError);
    expect(() =>
      buildSupportedRunSpec("select", { ...base, seed: 1.5 }),
    ).toThrow(TypeError);
  });
});

describe("monotonic event state", () => {
  const event = (sequence: number, type = "progress"): RunEvent => ({
    sequence,
    event_type: type,
    payload: {},
    occurred_at: `2026-08-11T12:00:0${sequence}Z`,
  });
  it("sorts, deduplicates, and advances only to the greatest cursor", () => {
    const existing = [event(2), event(1)];
    const merged = mergeMonotonicEvents(existing, [
      event(2, "stale-duplicate"),
      event(4),
      event(3),
    ]);
    expect(merged.map(({ sequence }) => sequence)).toEqual([1, 2, 3, 4]);
    expect(merged[1]?.event_type).toBe("progress");
    expect(nextEventCursor(merged)).toBe(4);
    expect(nextEventCursor([])).toBe(0);
  });

  it("identifies every terminal and non-terminal state", () => {
    expect(
      ["cancelled", "succeeded", "failed"].every((state) =>
        isTerminalRun(state as never),
      ),
    ).toBe(true);
    expect(
      ["draft", "queued", "provisioning", "running", "cancelling"].some(
        (state) => isTerminalRun(state as never),
      ),
    ).toBe(false);
  });

  it("parses only the closed progress projection and preserves reconnect order", () => {
    const payload = {
      schema_version: 1,
      activity_attempt: 1,
      sequence: 0,
      phase: "training",
      completed: 25,
      total: 100,
      coverage: null,
      accepted_count: null,
    };
    const parsed = parseEvents([
      {
        sequence: 4,
        event_type: "run.progress",
        payload,
        occurred_at: "2026-08-11T12:00:04Z",
      },
    ]);
    expect(parseRunProgress(parsed[0] as RunEvent)).toEqual(payload);
    const reconnected = mergeMonotonicEvents(parsed, [
      event(6, "run.succeeded"),
      {
        sequence: 5,
        event_type: "run.progress",
        payload: { ...payload, sequence: 1, completed: 100 },
        occurred_at: "2026-08-11T12:00:05Z",
      },
      parsed[0] as RunEvent,
    ]);
    expect(reconnected.map((item) => item.sequence)).toEqual([4, 5, 6]);
    expect(nextEventCursor(reconnected)).toBe(6);
  });

  it("rejects secret-bearing, malformed, and non-finite progress payloads", () => {
    const baseProgress = {
      schema_version: 1,
      activity_attempt: 1,
      sequence: 0,
      phase: "generating",
      completed: 1,
      total: 2,
      coverage: 0.5,
      accepted_count: 1,
    };
    const eventValue = (payload: Record<string, unknown>) => [
      {
        sequence: 4,
        event_type: "run.progress",
        payload,
        occurred_at: "2026-08-11T12:00:04Z",
      },
    ];
    expect(() =>
      parseEvents(
        eventValue({ ...baseProgress, api_key: "must-never-render" }),
      ),
    ).toThrow();
    expect(() =>
      parseEvents(eventValue({ ...baseProgress, completed: 3 })),
    ).toThrow();
    expect(() =>
      parseEvents(eventValue({ ...baseProgress, coverage: Number.NaN })),
    ).toThrow();
    expect(() =>
      parseEvents(eventValue({ ...baseProgress, sequence: 128 })),
    ).toThrow();
    expect(() =>
      parseEvents(eventValue({ ...baseProgress, activity_attempt: 101 })),
    ).toThrow();
    expect(() =>
      parseEvents(eventValue({ ...baseProgress, total: 10_001 })),
    ).toThrow();
  });
});

describe("durable job HTTP transport", () => {
  const uuid = "123e4567-e89b-42d3-a456-426614174000";
  const projectId = "123e4567-e89b-42d3-a456-426614174001";
  const hash = "a".repeat(64);
  const run = {
    id: uuid,
    organization_id: projectId,
    project_id: projectId,
    corpus_version_id: null,
    parent_run_id: null,
    kind: "evaluate",
    state: "queued",
    attempt: 1,
    spec: {},
    spec_sha256: hash,
    outbox_state: "pending",
    cancellation_requested_at: null,
    created_at: "2026-08-11T12:00:00Z",
    result_summary: null,
    failure_code: null,
  };
  const json = (value: unknown, status = 200) =>
    new Response(JSON.stringify(value), {
      status,
      headers: { "content-type": "application/json" },
    });

  it("calls list, detail, events, submit, cancel, and retry endpoints", async () => {
    fetchMock
      .mockResolvedValueOnce(json([run]))
      .mockResolvedValueOnce(json(run))
      .mockResolvedValueOnce(
        json([
          {
            sequence: 1,
            event_type: "queued",
            payload: {},
            occurred_at: "2026-08-11T12:00:01Z",
          },
        ]),
      )
      .mockResolvedValueOnce(json(run, 201))
      .mockResolvedValueOnce(json({ ...run, state: "cancelling" }, 202))
      .mockResolvedValueOnce(
        json(
          { ...run, id: "123e4567-e89b-42d3-a456-426614174009", attempt: 2 },
          201,
        ),
      );
    await expect(
      jobsApi.list({ state: "queued", kind: "evaluate", offset: 0, limit: 50 }),
    ).resolves.toHaveLength(1);
    await expect(jobsApi.get(uuid)).resolves.toMatchObject({ id: uuid });
    await expect(jobsApi.events(uuid, 0)).resolves.toHaveLength(1);
    await expect(
      jobsApi.submit(
        {
          project_id: projectId,
          corpus_version_id: null,
          kind: "evaluate",
          spec: {},
        },
        "submit-key",
      ),
    ).resolves.toMatchObject({ state: "queued" });
    await expect(jobsApi.cancel(uuid)).resolves.toMatchObject({
      state: "cancelling",
    });
    await expect(jobsApi.retry(uuid, "retry-key")).resolves.toMatchObject({
      attempt: 2,
    });
    expect(
      new Headers(fetchMock.mock.calls[3]?.[1]?.headers).get("Idempotency-Key"),
    ).toBe("submit-key");
    expect(
      new Headers(fetchMock.mock.calls[5]?.[1]?.headers).get("Idempotency-Key"),
    ).toBe("retry-key");
  });

  it("rejects malformed identifiers and idempotency keys before dispatch", () => {
    expect(() => jobsApi.get("../escape")).toThrow(TypeError);
    expect(() =>
      jobsApi.submit(
        {
          project_id: projectId,
          corpus_version_id: null,
          kind: "evaluate",
          spec: {},
        },
        "bad key",
      ),
    ).toThrow(TypeError);
    expect(() => jobsApi.retry(uuid, "")).toThrow(TypeError);
    expect(() => jobsApi.retry(uuid, "x".repeat(129))).toThrow(TypeError);
  });
});
