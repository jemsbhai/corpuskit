import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("@/lib/browser-auth", () => ({
  authenticatedFetch: (input: RequestInfo | URL, init?: RequestInit) =>
    fetch(input, init),
}));

import { InventoryAnalysisWorkbench } from "@/components/inventory-analysis-workbench";

afterEach(() => vi.unstubAllGlobals());

describe("InventoryAnalysisWorkbench", () => {
  it("disables inventory work safely when the PHOIBLE cache is absent", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(
          JSON.stringify({
            cache_available: false,
            loaded: false,
            revision: "b".repeat(40),
            sha256: "c".repeat(64),
            stats: null,
          }),
        ),
      ),
    );
    render(<InventoryAnalysisWorkbench />);
    expect(
      await screen.findByRole("heading", {
        name: "PHOIBLE data is not provisioned",
      }),
    ).toBeInTheDocument();
    expect(screen.getByText(/never downloads datasets/i)).toBeInTheDocument();
  });

  it("supports keyboard tab navigation and a live trajectory result", async () => {
    const user = userEvent.setup();
    vi.stubGlobal(
      "fetch",
      vi.fn().mockImplementation((path: string) => {
        if (path.endsWith("/phonology/status"))
          return Promise.resolve(
            new Response(
              JSON.stringify({
                cache_available: true,
                loaded: true,
                revision: "b".repeat(40),
                sha256: "c".repeat(64),
                stats: null,
              }),
            ),
          );
        return Promise.resolve(
          new Response(
            JSON.stringify({
              unit: "phoneme",
              target_size: 2,
              coverages: [0.5, 1],
              gains: [1, 1],
              snapshots: [
                {
                  sentence_index: 0,
                  coverage: 0.5,
                  covered_count: 1,
                  new_units_count: 1,
                  new_units: ["p"],
                },
                {
                  sentence_index: 1,
                  coverage: 1,
                  covered_count: 2,
                  new_units_count: 1,
                  new_units: ["b"],
                },
              ],
            }),
          ),
        );
      }),
    );
    render(<InventoryAnalysisWorkbench />);
    await screen.findByRole("heading", { name: "Language & segment explorer" });
    const inventoryTab = screen.getByRole("tab", {
      name: /inventory explorer/i,
    });
    inventoryTab.focus();
    await user.keyboard("{End}");
    expect(
      screen.getByRole("tab", { name: /coverage trajectory/i }),
    ).toHaveFocus();
    await user.type(
      screen.getByRole("textbox", { name: /phoneme sequences/i }),
      "p\np b",
    );
    await user.type(
      screen.getByRole("textbox", { name: /target units/i }),
      "p b",
    );
    await user.click(screen.getByRole("button", { name: "Build trajectory" }));
    expect(
      await screen.findByRole("heading", { name: "100.0% final coverage" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("table", { name: /coverage after each sentence/i }),
    ).toHaveTextContent("50.0%");
  });

  it("shows local validation without making an analysis request", async () => {
    const user = userEvent.setup();
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({
          cache_available: true,
          loaded: true,
          revision: "b".repeat(40),
          sha256: "c".repeat(64),
          stats: null,
        }),
      ),
    );
    vi.stubGlobal("fetch", fetchMock);
    render(<InventoryAnalysisWorkbench />);
    await screen.findByRole("heading", { name: "Language & segment explorer" });
    await user.click(screen.getByRole("tab", { name: /error rates/i }));
    await user.type(
      screen.getByRole("textbox", { name: /^references/i }),
      "one\ntwo",
    );
    await user.type(
      screen.getByRole("textbox", { name: /^hypotheses/i }),
      "one",
    );
    await user.click(
      screen.getByRole("button", { name: "Calculate error rates" }),
    );
    expect(screen.getByRole("alert")).toHaveTextContent(/same number/i);
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
  });
});
