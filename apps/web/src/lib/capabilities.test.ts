import { afterEach, describe, expect, it, vi } from "vitest";

import {
  controlsForCapability,
  fetchCapabilityCatalog,
  getFallbackCatalog,
} from "@/lib/capabilities";

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("capability catalog", () => {
  it("rejects malformed API records", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(JSON.stringify({ capabilities: [{ id: "bad" }] }), {
          status: 200,
        }),
      ),
    );

    await expect(fetchCapabilityCatalog()).rejects.toThrow(
      /invalid capability records/i,
    );
  });

  it("maps the backend CapabilityReport contract", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(
          JSON.stringify({
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
            ],
          }),
          { status: 200 },
        ),
      ),
    );

    const catalog = await fetchCapabilityCatalog();
    expect(catalog).toMatchObject({
      source: "api",
      engineVersion: "0.1.7",
      checkedAt: "2026-08-11T12:00:00Z",
      capabilities: [
        {
          id: "corpusgen-core",
          name: "CorpusGen engine",
          description: "Ready.",
          status: "available",
          profile: "Interactive CPU",
          required: true,
          version: "0.1.7",
        },
      ],
    });
  });

  it("uses honest planned states in fallback data", () => {
    const catalog = getFallbackCatalog();

    expect(catalog.source).toBe("fallback");
    expect(catalog.capabilities.length).toBeGreaterThanOrEqual(6);
    expect(
      catalog.capabilities.every(
        (capability) => capability.status === "planned",
      ),
    ).toBe(true);
    expect(
      catalog.capabilities
        .map((capability) => capability.reason ?? "")
        .join(" "),
    ).not.toMatch(/worker reports? availability/i);
  });

  it("maps repository detection to its external-provider durable controls", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(
          JSON.stringify({
            checked_at: "2026-08-11T12:00:00Z",
            ready: true,
            missing_required: [],
            checks: [
              {
                id: "repository",
                state: "available",
                label: "Repository and Hugging Face import",
                detail: "Required optional modules are installed.",
                remediation: null,
                version: null,
                required: false,
              },
            ],
          }),
          { status: 200 },
        ),
      ),
    );

    const catalog = await fetchCapabilityCatalog();
    expect(catalog.capabilities[0]).toMatchObject({
      id: "repository",
      profile: "External provider",
    });
    expect(controlsForCapability("repository")).toEqual([
      expect.objectContaining({
        href: "/advanced",
        label: "Repository runtime",
      }),
    ]);
  });

  it("maps backend checks to affected controls without inventing unknown mappings", () => {
    expect(controlsForCapability("phoible").map(({ href }) => href)).toContain(
      "/inventory",
    );
    expect(controlsForCapability("optimization")).toEqual([
      expect.objectContaining({ label: "ILP and NSGA-II" }),
    ]);
    expect(controlsForCapability("unknown")).toEqual([]);
  });
});
