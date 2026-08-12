import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("@/lib/browser-auth", () => ({ authenticatedFetch: vi.fn() }));

import { ApiContractError } from "@/lib/api-client";
import {
  parseReplayStatus,
  replaysApi,
  type ReplayStatus,
} from "@/lib/replays";
import { authenticatedFetch } from "@/lib/browser-auth";

const fetchMock = vi.mocked(authenticatedFetch);
const replayId = "123e4567-e89b-42d3-a456-426614174000";
const sourceId = "123e4567-e89b-42d3-a456-426614174001";
const projectId = "123e4567-e89b-42d3-a456-426614174002";
const sourceManifestId = "123e4567-e89b-42d3-a456-426614174003";
const observedManifestId = "123e4567-e89b-42d3-a456-426614174004";

const queued: ReplayStatus = {
  replay_run_id: replayId,
  source_run_id: sourceId,
  source_manifest_artifact_id: sourceManifestId,
  expected_manifest_sha256: "a".repeat(64),
  observed_manifest_artifact_id: null,
  classification: "exact",
  lifecycle: "queued",
  comparison: null,
};
const compared: ReplayStatus = {
  ...queued,
  observed_manifest_artifact_id: observedManifestId,
  lifecycle: "compared",
  comparison: {
    classification: "exact",
    verdict: "exact-match",
    replay_inputs_match: true,
    outputs_match: true,
    differences: [],
  },
};

const json = (value: unknown, status = 200) =>
  new Response(JSON.stringify(value), {
    status,
    headers: { "content-type": "application/json" },
  });

afterEach(() => fetchMock.mockReset());

describe("replay response contracts", () => {
  it("parses queued, unavailable, and completed comparison projections", () => {
    expect(parseReplayStatus(queued)).toEqual(queued);
    expect(
      parseReplayStatus({
        ...queued,
        classification: "best-effort",
        lifecycle: "unavailable",
      }),
    ).toMatchObject({
      classification: "best-effort",
      lifecycle: "unavailable",
    });
    expect(parseReplayStatus(compared)).toEqual(compared);
    expect(
      parseReplayStatus({
        ...compared,
        classification: "nonreproducible",
        comparison: {
          ...compared.comparison,
          classification: "nonreproducible",
          verdict: "nonreproducible",
          replay_inputs_match: false,
          outputs_match: false,
          differences: ["worker", "output_digests"],
        },
      }),
    ).toMatchObject({ classification: "nonreproducible" });
  });

  it.each([
    null,
    { ...queued, replay_run_id: "bad" },
    { ...queued, source_run_id: "bad" },
    { ...queued, source_manifest_artifact_id: "bad" },
    { ...queued, expected_manifest_sha256: 1 },
    { ...queued, expected_manifest_sha256: "x" },
    { ...queued, observed_manifest_artifact_id: "bad" },
    { ...queued, classification: 1 },
    { ...queued, classification: "sometimes" },
    { ...queued, lifecycle: 1 },
    { ...queued, lifecycle: "finished" },
    { ...queued, comparison: undefined },
    { ...queued, lifecycle: "compared" },
    { ...queued, observed_manifest_artifact_id: observedManifestId },
    { ...compared, lifecycle: "running" },
    { ...compared, observed_manifest_artifact_id: null },
    {
      ...compared,
      classification: "best-effort",
      comparison: { ...compared.comparison, classification: "exact" },
    },
  ])("rejects malformed replay status %#", (value) => {
    expect(() => parseReplayStatus(value)).toThrow(ApiContractError);
  });

  it.each([
    { classification: 1 },
    { classification: "sometimes" },
    { verdict: 1 },
    { verdict: "unknown" },
    { replay_inputs_match: "yes" },
    { outputs_match: "yes" },
    { differences: "worker" },
    { differences: Array.from({ length: 65 }, () => "worker") },
    { differences: ["unsafe field"] },
  ])("rejects malformed comparison fields %#", (change) => {
    expect(() =>
      parseReplayStatus({
        ...compared,
        comparison: { ...compared.comparison, ...change },
      }),
    ).toThrow(ApiContractError);
  });
});

describe("replay HTTP transport", () => {
  it("submits an empty-body idempotent request and reads the status projection", async () => {
    const controller = new AbortController();
    fetchMock
      .mockResolvedValueOnce(json({ replay: queued, created: true }, 201))
      .mockResolvedValueOnce(json(compared));

    await expect(
      replaysApi.submit(
        projectId,
        sourceId,
        "ui-replay-key",
        controller.signal,
      ),
    ).resolves.toEqual({ replay: queued, created: true });
    await expect(replaysApi.get(replayId, controller.signal)).resolves.toEqual(
      compared,
    );

    expect(fetchMock.mock.calls[0]?.[0]).toBe(
      `/api/v1/projects/${projectId}/runs/${sourceId}/replays`,
    );
    const post = fetchMock.mock.calls[0]?.[1];
    const headers = new Headers(post?.headers);
    expect(post?.method).toBe("POST");
    expect(post?.body).toBeUndefined();
    expect(post?.signal).toBe(controller.signal);
    expect(headers.get("Idempotency-Key")).toBe("ui-replay-key");
    expect(headers.get("content-type")).toBeNull();
    expect(fetchMock.mock.calls[1]?.[0]).toBe(`/api/v1/replays/${replayId}`);
  });

  it("supports omitted abort signals and an idempotently restored response", async () => {
    fetchMock
      .mockResolvedValueOnce(json({ replay: queued, created: false }))
      .mockResolvedValueOnce(json(queued));
    await expect(
      replaysApi.submit(projectId, sourceId, "restored-key"),
    ).resolves.toMatchObject({ created: false });
    await expect(replaysApi.get(replayId)).resolves.toMatchObject({
      lifecycle: "queued",
    });
  });

  it.each([
    null,
    {},
    { replay: queued, created: "yes" },
    { replay: { ...queued, replay_run_id: "bad" }, created: true },
  ])("rejects malformed creation responses %#", async (value) => {
    fetchMock.mockResolvedValueOnce(json(value));
    await expect(
      replaysApi.submit(projectId, sourceId, "contract-key"),
    ).rejects.toThrow(ApiContractError);
  });

  it("rejects unsafe identifiers and idempotency keys before dispatch", () => {
    expect(() => replaysApi.get("../escape")).toThrow(TypeError);
    expect(() => replaysApi.submit("../project", sourceId, "key")).toThrow(
      TypeError,
    );
    expect(() => replaysApi.submit(projectId, "../source", "key")).toThrow(
      TypeError,
    );
    expect(() => replaysApi.submit(projectId, sourceId, "bad key")).toThrow(
      TypeError,
    );
    expect(() =>
      replaysApi.submit(projectId, sourceId, "x".repeat(129)),
    ).toThrow(TypeError);
    expect(fetchMock).not.toHaveBeenCalled();
  });
});
