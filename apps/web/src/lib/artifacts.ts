import {
  ApiContractError,
  isNonnegativeInteger,
  isRecord,
  isUuid,
  pathIdentifier,
  queryString,
  requestDownload,
  requestJson,
  requestVoid,
} from "@/lib/api-client";
import { sha256Hex } from "@/lib/workbench-input";

export const maximumArtifactBytes = 10 * 1024 * 1024;
export const maximumPromptArtifactBytes = 8 * 1024 * 1024;
export type PublicUploadKind = "corpus-text" | "prompt-set";

export interface ArtifactRecord {
  readonly id: string;
  readonly project_id: string;
  readonly run_id: string | null;
  readonly kind: string;
  readonly sha256: string;
  readonly size_bytes: number;
  readonly media_type: string;
  readonly filename: string;
  readonly state: "active" | "tombstoned" | "deleted";
  readonly retention_until: string;
  readonly created_at: string;
}

export interface SignedDownload {
  readonly url: string;
  readonly expires_at: string;
}

export function parseArtifact(value: unknown): ArtifactRecord {
  if (
    !isRecord(value) ||
    !isUuid(value.id) ||
    !isUuid(value.project_id) ||
    (value.run_id !== null && !isUuid(value.run_id)) ||
    typeof value.kind !== "string" ||
    typeof value.sha256 !== "string" ||
    !/^[0-9a-f]{64}$/u.test(value.sha256) ||
    !isNonnegativeInteger(value.size_bytes) ||
    typeof value.media_type !== "string" ||
    typeof value.filename !== "string" ||
    (value.state !== "active" &&
      value.state !== "tombstoned" &&
      value.state !== "deleted") ||
    typeof value.retention_until !== "string" ||
    typeof value.created_at !== "string"
  )
    throw new ApiContractError();
  return value as unknown as ArtifactRecord;
}

export function parseArtifacts(value: unknown): readonly ArtifactRecord[] {
  if (!Array.isArray(value)) throw new ApiContractError();
  return value.map(parseArtifact);
}

export function parseArtifactCreation(value: unknown): {
  readonly artifact: ArtifactRecord;
  readonly created: boolean;
} {
  if (!isRecord(value) || typeof value.created !== "boolean")
    throw new ApiContractError();
  return { artifact: parseArtifact(value.artifact), created: value.created };
}

export function parseSignedDownload(value: unknown): SignedDownload {
  if (
    !isRecord(value) ||
    typeof value.url !== "string" ||
    typeof value.expires_at !== "string"
  )
    throw new ApiContractError();
  let url: URL;
  try {
    url = new URL(value.url);
  } catch {
    throw new ApiContractError();
  }
  const loopback =
    url.hostname === "127.0.0.1" ||
    url.hostname === "localhost" ||
    url.hostname === "::1" ||
    url.hostname === "[::1]";
  if (
    (url.protocol !== "https:" && !(url.protocol === "http:" && loopback)) ||
    url.username ||
    url.password
  )
    throw new ApiContractError();
  return { url: url.toString(), expires_at: value.expires_at };
}

export async function verifiedArtifactDownload(
  projectId: string,
  artifact: ArtifactRecord,
  signal?: AbortSignal,
): Promise<Uint8Array> {
  if (
    artifact.project_id !== projectId ||
    artifact.size_bytes > maximumArtifactBytes
  )
    throw new ApiContractError();
  const response = await requestDownload(
    `/api/v1/projects/${pathIdentifier(projectId)}/artifacts/${pathIdentifier(artifact.id)}/download`,
    { signal },
  );
  if (response.bytes.byteLength !== artifact.size_bytes)
    throw new ApiContractError();
  const digest = await sha256Hex(Uint8Array.from(response.bytes).buffer);
  const headerDigest = response.headers.get("x-content-sha256");
  if (
    digest !== artifact.sha256 ||
    (headerDigest !== null && headerDigest !== artifact.sha256)
  )
    throw new ApiContractError();
  return response.bytes;
}

export const artifactsApi = {
  list(projectId: string, offset = 0, limit = 100, signal?: AbortSignal) {
    return requestJson(
      `/api/v1/projects/${pathIdentifier(projectId)}/artifacts${queryString({ offset, limit })}`,
      parseArtifacts,
      { signal },
    );
  },
  get(projectId: string, artifactId: string, signal?: AbortSignal) {
    return requestJson(
      `/api/v1/projects/${pathIdentifier(projectId)}/artifacts/${pathIdentifier(artifactId)}`,
      parseArtifact,
      { signal },
    );
  },
  upload(
    projectId: string,
    file: File,
    expectedSha256: string,
    kind: PublicUploadKind = "corpus-text",
    signal?: AbortSignal,
  ) {
    if (
      file.size > maximumArtifactBytes ||
      (kind === "prompt-set" && file.size > maximumPromptArtifactBytes) ||
      !/^[0-9a-f]{64}$/u.test(expectedSha256)
    )
      throw new TypeError("The artifact does not meet upload limits.");
    const form = new FormData();
    form.set("file", file);
    form.set("kind", kind);
    form.set("expected_sha256", expectedSha256);
    return requestJson(
      `/api/v1/projects/${pathIdentifier(projectId)}/artifacts`,
      parseArtifactCreation,
      { method: "POST", body: form, signal },
    );
  },
  sign(
    projectId: string,
    artifactId: string,
    expiresSeconds: number,
    signal?: AbortSignal,
  ) {
    return requestJson(
      `/api/v1/projects/${pathIdentifier(projectId)}/artifacts/${pathIdentifier(artifactId)}/download-url${queryString({ expires_seconds: expiresSeconds })}`,
      parseSignedDownload,
      { method: "POST", signal },
    );
  },
  async remove(
    projectId: string,
    artifactId: string,
    signal?: AbortSignal,
  ): Promise<void> {
    await requestVoid(
      `/api/v1/projects/${pathIdentifier(projectId)}/artifacts/${pathIdentifier(artifactId)}`,
      { method: "DELETE", signal },
    );
  },
};
