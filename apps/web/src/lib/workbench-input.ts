export interface WeightedUnit {
  readonly unit: string;
  readonly weight: number;
}

export function nonEmptyLines(value: string, maximum = 2_000): string[] {
  const lines = value
    .split(/\r?\n/u)
    .map((item) => item.trim())
    .filter(Boolean);
  if (lines.length > maximum) throw new RangeError("Too many input rows.");
  return lines;
}

export function orderedLines(value: string, maximum = 500): string[] {
  const lines = value.split(/\r?\n/u).map((item) => item.trim());
  if (lines.length > maximum) throw new RangeError("Too many input rows.");
  return lines;
}

export function uniqueTokens(value: string, maximum = 256): string[] {
  const tokens = value
    .split(/[,\s]+/u)
    .map((item) => item.trim())
    .filter(Boolean);
  const result = Array.from(new Set(tokens));
  if (result.length > maximum) throw new RangeError("Too many phonetic units.");
  return result;
}

export function phonemeRows(value: string, maximum = 2_000): string[][] {
  return nonEmptyLines(value, maximum).map((line) => uniqueTokens(line, 1_000));
}

export function weightedUnits(
  value: string,
  options: { readonly allowZero?: boolean; readonly maximum?: number } = {},
): WeightedUnit[] {
  const rows = nonEmptyLines(value, options.maximum ?? 10_000);
  const seen = new Set<string>();
  return rows.map((row) => {
    const parts = row.split(",").map((part) => part.trim());
    const unit = parts[0] ?? "";
    const weight = Number(parts[1]);
    const minimum = options.allowZero ? 0 : Number.MIN_VALUE;
    if (
      parts.length !== 2 ||
      !unit ||
      unit.length > 64 ||
      seen.has(unit) ||
      !Number.isFinite(weight) ||
      weight < minimum ||
      weight > 1_000_000
    ) {
      throw new TypeError("Weights must use unique unit,number rows.");
    }
    seen.add(unit);
    return { unit, weight };
  });
}

export function pairedLines(
  left: string,
  right: string,
  maximum = 500,
): readonly [string[], string[]] {
  const leftRows = orderedLines(left, maximum);
  const rightRows = orderedLines(right, maximum);
  if (leftRows.length !== rightRows.length) {
    throw new TypeError("Both inputs must contain the same number of rows.");
  }
  return [leftRows, rightRows];
}

export function saveBytes(
  filename: string,
  bytes: BlobPart,
  mediaType: string,
): void {
  if (!/^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/u.test(filename)) {
    throw new TypeError("Invalid download filename.");
  }
  const url = URL.createObjectURL(new Blob([bytes], { type: mediaType }));
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  anchor.rel = "noopener";
  anchor.click();
  URL.revokeObjectURL(url);
}

export function saveJson(filename: string, value: unknown): void {
  saveBytes(filename, JSON.stringify(value, null, 2), "application/json");
}

export async function sha256Hex(bytes: ArrayBuffer): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", bytes);
  return Array.from(new Uint8Array(digest), (value) =>
    value.toString(16).padStart(2, "0"),
  ).join("");
}
