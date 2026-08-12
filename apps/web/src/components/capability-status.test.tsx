import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { CapabilityStatus } from "@/components/capability-status";

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("CapabilityStatus", () => {
  it("renders validated live API status", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(
          JSON.stringify({
            environment: "test-cluster",
            engineVersion: "0.1.7",
            checkedAt: "2026-08-11T12:00:00Z",
            capabilities: [
              {
                id: "evaluation",
                name: "Coverage evaluation",
                description: "Coverage and quality metrics.",
                status: "available",
                profile: "CPU",
              },
            ],
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        ),
      ),
    );

    render(<CapabilityStatus />);

    expect(
      await screen.findByText("API process detection"),
    ).toBeInTheDocument();
    expect(
      screen.getByText("1 of 1 checks available in the API process"),
    ).toBeInTheDocument();
    expect(
      screen.getByText(
        /test-cluster \/ CorpusGen 0.1.7.*does not probe durable worker health/i,
      ),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/does not establish that a Temporal worker is running/i),
    ).toBeInTheDocument();
  });

  it("falls back to clearly labeled roadmap data when the API fails", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new Error("offline")));

    render(<CapabilityStatus />);

    expect(await screen.findByText("Preview data")).toBeInTheDocument();
    expect(
      screen.getByText("The capability API is not connected"),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/illustrative roadmap, not claims/i),
    ).toBeInTheDocument();
    expect(screen.getAllByText("Planned").length).toBeGreaterThan(0);
    expect(
      screen.queryByText(/worker reports? availability/i),
    ).not.toBeInTheDocument();
  });

  it("refreshes deployment capability checks on demand", async () => {
    const user = userEvent.setup();
    const fetchMock = vi.fn().mockImplementation(async () =>
      Promise.resolve(
        new Response(
          JSON.stringify({
            environment: "test-cluster",
            checkedAt: "2026-08-11T12:00:00Z",
            capabilities: [
              {
                id: "evaluation",
                name: "Coverage evaluation",
                description: "Coverage and quality metrics.",
                status: "available",
                profile: "CPU",
              },
            ],
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        ),
      ),
    );
    vi.stubGlobal("fetch", fetchMock);
    render(<CapabilityStatus />);

    await user.click(
      await screen.findByRole("button", {
        name: "Refresh capability checks",
      }),
    );
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });
});
