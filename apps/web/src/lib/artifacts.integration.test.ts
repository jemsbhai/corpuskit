import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@/lib/browser-auth", () => ({ authenticatedFetch: vi.fn() }));

import { ApiContractError } from "@/lib/api-client";
import {
  artifactsApi,
  maximumArtifactBytes,
  parseArtifact,
  parseArtifactCreation,
  parseSignedDownload,
  verifiedArtifactDownload,
  type ArtifactRecord,
} from "@/lib/artifacts";
import { authenticatedFetch } from "@/lib/browser-auth";

const fetchMock = vi.mocked(authenticatedFetch);
const sha = "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad";
const artifact: ArtifactRecord = {
  id: "123e4567-e89b-42d3-a456-426614174000",
  project_id: "123e4567-e89b-42d3-a456-426614174001",
  run_id: null,
  kind: "corpus-text",
  sha256: sha,
  size_bytes: 3,
  media_type: "text/plain",
  filename: "corpus.txt",
  state: "active",
  retention_until: "2026-09-11T12:00:00Z",
  created_at: "2026-08-11T12:00:00Z",
};

beforeEach(() => {
  vi.spyOn(crypto.subtle, "digest").mockResolvedValue(
    Uint8Array.from(
      sha.match(/.{2}/gu)!.map((part) => Number.parseInt(part, 16)),
    ).buffer,
  );
});
afterEach(() => {
  fetchMock.mockReset();
  vi.restoreAllMocks();
});

describe("full-object artifact verification", () => {
  it("accepts bytes only when size, metadata digest, and header digest agree", async () => {
    fetchMock.mockResolvedValueOnce(
      new Response(new TextEncoder().encode("abc"), {
        headers: { "x-content-sha256": sha },
      }),
    );
    const result = await verifiedArtifactDownload(
      artifact.project_id,
      artifact,
    );
    expect(Array.from(result)).toEqual([97, 98, 99]);
  });

  it("accepts a missing optional digest header after computing the body hash", async () => {
    fetchMock.mockResolvedValueOnce(
      new Response(new TextEncoder().encode("abc")),
    );
    await expect(
      verifiedArtifactDownload(artifact.project_id, artifact),
    ).resolves.toHaveLength(3);
  });

  it("fails closed on project mismatch, oversized metadata, size mismatch, or digest mismatch", async () => {
    await expect(
      verifiedArtifactDownload(
        "123e4567-e89b-42d3-a456-426614174009",
        artifact,
      ),
    ).rejects.toBeInstanceOf(ApiContractError);
    await expect(
      verifiedArtifactDownload(artifact.project_id, {
        ...artifact,
        size_bytes: maximumArtifactBytes + 1,
      }),
    ).rejects.toBeInstanceOf(ApiContractError);
    fetchMock.mockResolvedValueOnce(
      new Response(new TextEncoder().encode("ab")),
    );
    await expect(
      verifiedArtifactDownload(artifact.project_id, artifact),
    ).rejects.toBeInstanceOf(ApiContractError);
    fetchMock.mockResolvedValueOnce(
      new Response(new TextEncoder().encode("abc")),
    );
    await expect(
      verifiedArtifactDownload(artifact.project_id, {
        ...artifact,
        sha256: "f".repeat(64),
      }),
    ).rejects.toBeInstanceOf(ApiContractError);
    fetchMock.mockResolvedValueOnce(
      new Response(new TextEncoder().encode("abc"), {
        headers: { "x-content-sha256": "f".repeat(64) },
      }),
    );
    await expect(
      verifiedArtifactDownload(artifact.project_id, artifact),
    ).rejects.toBeInstanceOf(ApiContractError);
  });
});

describe("artifact transport and strict metadata", () => {
  const json = (value: unknown, status = 200) =>
    new Response(JSON.stringify(value), {
      status,
      headers: { "content-type": "application/json" },
    });

  it("calls list, detail, upload, signing, and deletion contracts", async () => {
    fetchMock
      .mockResolvedValueOnce(json([artifact]))
      .mockResolvedValueOnce(json(artifact))
      .mockResolvedValueOnce(json({ artifact, created: true }, 201))
      .mockResolvedValueOnce(json({ artifact, created: true }, 201))
      .mockResolvedValueOnce(
        json({
          url: "https://objects.example.test/file?signature=x",
          expires_at: "2026-08-11T12:05:00Z",
        }),
      )
      .mockResolvedValueOnce(new Response(null, { status: 204 }));
    await expect(artifactsApi.list(artifact.project_id)).resolves.toHaveLength(
      1,
    );
    await expect(
      artifactsApi.get(artifact.project_id, artifact.id),
    ).resolves.toMatchObject({ id: artifact.id });
    await expect(
      artifactsApi.upload(
        artifact.project_id,
        new File(["abc"], "corpus.txt", { type: "text/plain" }),
        sha,
      ),
    ).resolves.toMatchObject({ created: true });
    await expect(
      artifactsApi.upload(
        artifact.project_id,
        new File(
          [
            '{"prompts":["Cover p"],"schema_id":"corpuskit.phon-rl-prompt-artifact.v1"}',
          ],
          "prompts.json",
          { type: "application/json" },
        ),
        sha,
        "prompt-set",
      ),
    ).resolves.toMatchObject({ created: true });
    await expect(
      artifactsApi.sign(artifact.project_id, artifact.id, 300),
    ).resolves.toMatchObject({ expires_at: "2026-08-11T12:05:00Z" });
    await expect(
      artifactsApi.remove(artifact.project_id, artifact.id),
    ).resolves.toBeUndefined();
    expect(fetchMock.mock.calls.map(([path]) => path)).toEqual(
      expect.arrayContaining([
        expect.stringContaining("/artifacts?offset=0&limit=100"),
        expect.stringContaining("download-url?expires_seconds=300"),
      ]),
    );
    const corpusForm = fetchMock.mock.calls[2]?.[1]?.body;
    const promptForm = fetchMock.mock.calls[3]?.[1]?.body;
    expect(corpusForm).toBeInstanceOf(FormData);
    expect(promptForm).toBeInstanceOf(FormData);
    expect((corpusForm as FormData).get("kind")).toBe("corpus-text");
    expect((promptForm as FormData).get("kind")).toBe("prompt-set");
  });

  it("rejects unsafe upload metadata before network access", () => {
    const large = new File(
      [new Uint8Array(maximumArtifactBytes + 1)],
      "large.txt",
      { type: "text/plain" },
    );
    expect(() => artifactsApi.upload(artifact.project_id, large, sha)).toThrow(
      TypeError,
    );
    expect(() =>
      artifactsApi.upload(artifact.project_id, large, sha, "prompt-set"),
    ).toThrow(TypeError);
    expect(() =>
      artifactsApi.upload(artifact.project_id, new File(["x"], "x.txt"), "bad"),
    ).toThrow(TypeError);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it.each([
    null,
    { ...artifact, id: "bad" },
    { ...artifact, project_id: "bad" },
    { ...artifact, run_id: "bad" },
    { ...artifact, kind: 1 },
    { ...artifact, sha256: "bad" },
    { ...artifact, size_bytes: -1 },
    { ...artifact, media_type: 1 },
    { ...artifact, filename: 1 },
    { ...artifact, state: "unknown" },
    { ...artifact, retention_until: 1 },
    { ...artifact, created_at: 1 },
  ])("rejects malformed artifact metadata", (value) =>
    expect(() => parseArtifact(value)).toThrow(ApiContractError),
  );

  it("rejects malformed creation and signed-link envelopes", () => {
    expect(() => parseArtifactCreation(null)).toThrow(ApiContractError);
    expect(() => parseArtifactCreation({ artifact, created: "yes" })).toThrow(
      ApiContractError,
    );
    for (const value of [
      null,
      { url: 1, expires_at: "x" },
      { url: "not a url", expires_at: "x" },
      { url: "ftp://objects.example/file", expires_at: "x" },
      { url: "http://objects.example/file", expires_at: "x" },
      { url: "https://user:pass@objects.example/file", expires_at: "x" },
    ]) {
      expect(() => parseSignedDownload(value)).toThrow(ApiContractError);
    }
    expect(
      parseSignedDownload({
        url: "http://127.0.0.1:9000/file",
        expires_at: "x",
      }).url,
    ).toContain("127.0.0.1");
    expect(
      parseSignedDownload({ url: "http://[::1]:9000/file", expires_at: "x" })
        .url,
    ).toContain("::1");
  });
});
