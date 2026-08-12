import { afterEach, describe, expect, it, vi } from "vitest";

import {
  nonEmptyLines,
  orderedLines,
  pairedLines,
  phonemeRows,
  saveBytes,
  saveJson,
  sha256Hex,
  uniqueTokens,
  weightedUnits,
} from "@/lib/workbench-input";

afterEach(() => vi.restoreAllMocks());

describe("workbench input builders", () => {
  it("normalizes bounded line and token inputs while retaining ordered blanks", () => {
    expect(nonEmptyLines(" a \r\n\n b ")).toEqual(["a", "b"]);
    expect(orderedLines(" a\n\n b ")).toEqual(["a", "", "b"]);
    expect(uniqueTokens("p, b p\tɡ")).toEqual(["p", "b", "ɡ"]);
    expect(phonemeRows("p a\nb a")).toEqual([
      ["p", "a"],
      ["b", "a"],
    ]);
    expect(() => nonEmptyLines("a\nb", 1)).toThrow(RangeError);
    expect(() => orderedLines("a\nb", 1)).toThrow(RangeError);
    expect(() => uniqueTokens("a b", 1)).toThrow(RangeError);
  });

  it("parses strict unique weights and supported zero weights", () => {
    expect(weightedUnits("p,2\nb,0.5")).toEqual([
      { unit: "p", weight: 2 },
      { unit: "b", weight: 0.5 },
    ]);
    expect(weightedUnits("p,0", { allowZero: true })).toEqual([
      { unit: "p", weight: 0 },
    ]);
    for (const input of [
      "p",
      "p,nope",
      ",1",
      "p,0",
      "p,1\np,2",
      `${"x".repeat(65)},1`,
      "p,1000001",
    ]) {
      expect(() => weightedUnits(input)).toThrow(TypeError);
    }
  });

  it("pairs rows by exact count", () => {
    expect(pairedLines("a\nb", "x\ny")).toEqual([
      ["a", "b"],
      ["x", "y"],
    ]);
    expect(() => pairedLines("a", "x\ny")).toThrow(TypeError);
  });
});

describe("safe local exports", () => {
  it("creates and revokes a browser download", () => {
    const create = vi
      .spyOn(URL, "createObjectURL")
      .mockReturnValue("blob:test");
    const revoke = vi
      .spyOn(URL, "revokeObjectURL")
      .mockImplementation(() => undefined);
    const click = vi
      .spyOn(HTMLAnchorElement.prototype, "click")
      .mockImplementation(() => undefined);
    saveBytes("report.json", "{}", "application/json");
    saveJson("other.json", { ok: true });
    expect(create).toHaveBeenCalledTimes(2);
    expect(click).toHaveBeenCalledTimes(2);
    expect(revoke).toHaveBeenCalledWith("blob:test");
    expect(() => saveBytes("../secret", "x", "text/plain")).toThrow(TypeError);
  });

  it("computes the canonical SHA-256 hex digest", async () => {
    const bytes = new TextEncoder().encode("abc");
    await expect(sha256Hex(bytes.buffer)).resolves.toBe(
      "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad",
    );
  });
});
