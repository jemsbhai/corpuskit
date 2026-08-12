import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  quota: vi.fn(),
  auditEvents: vi.fn(),
  principal: vi.fn(),
}));

vi.mock("@/lib/projects", async () => {
  const actual =
    await vi.importActual<typeof import("@/lib/projects")>("@/lib/projects");
  return { ...actual, getCurrentPrincipal: mocks.principal };
});

vi.mock("@/lib/platform", async () => {
  const actual =
    await vi.importActual<typeof import("@/lib/platform")>("@/lib/platform");
  return {
    ...actual,
    platformApi: {
      quota: mocks.quota,
      auditEvents: mocks.auditEvents,
    },
  };
});

import { PlatformGovernance } from "@/components/platform-governance";

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
  actor_kind: "user" as const,
  actor_id: "user|demo",
  action: "run.submitted" as const,
  resource_type: "run" as const,
  resource_id: "123e4567-e89b-42d3-a456-426614174009",
  request_id: "request-7",
  occurred_at: "2026-08-11T12:00:00Z",
  metadata: { kind: "evaluate" },
  previous_hash: "a".repeat(64),
  event_hash: "b".repeat(64),
};

beforeEach(() => {
  vi.resetAllMocks();
  mocks.principal.mockResolvedValue({
    subject: "user|owner",
    organization_id: "123e4567-e89b-42d3-a456-426614174000",
    role: "owner",
    display_name: "Owner",
  });
  mocks.quota.mockResolvedValue(quota);
  mocks.auditEvents.mockResolvedValue({ events: [event], next_cursor: null });
});

describe("PlatformGovernance", () => {
  it("renders quota usage and immutable audit rows, then refreshes both", async () => {
    const user = userEvent.setup();
    render(<PlatformGovernance />);

    expect(
      await screen.findByRole("heading", { name: "Current quota usage" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("meter", { name: "Active CPU jobs quota usage" }),
    ).toHaveValue(1);
    expect(screen.getByText("run.submitted")).toBeInTheDocument();
    expect(screen.getByText("user: user|demo")).toBeInTheDocument();

    await user.click(
      screen.getByRole("button", { name: "Refresh platform data" }),
    );
    expect(mocks.quota).toHaveBeenCalledTimes(2);
    expect(mocks.auditEvents).toHaveBeenCalledTimes(2);
  });

  it("loads the next hash-linked page with the server cursor", async () => {
    const user = userEvent.setup();
    mocks.auditEvents
      .mockResolvedValueOnce({ events: [event], next_cursor: "6" })
      .mockResolvedValueOnce({
        events: [{ ...event, sequence: 6, event_hash: "c".repeat(64) }],
        next_cursor: null,
      });
    render(<PlatformGovernance />);

    await user.click(
      await screen.findByRole("button", { name: "Load next audit page" }),
    );
    expect(mocks.auditEvents).toHaveBeenLastCalledWith({
      cursor: "6",
      limit: 25,
    });
    expect(screen.getAllByRole("row")).toHaveLength(3);
  });

  it("sanitizes transport failures", async () => {
    mocks.quota.mockRejectedValueOnce(new Error("private database host"));
    render(<PlatformGovernance />);

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "temporarily unavailable",
    );
    expect(screen.getByRole("alert")).not.toHaveTextContent("database host");
  });

  it("explains the owner/admin boundary without requesting tenant data", async () => {
    mocks.principal.mockResolvedValueOnce({
      subject: "user|viewer",
      organization_id: "123e4567-e89b-42d3-a456-426614174000",
      role: "viewer",
      display_name: "Viewer",
    });
    render(<PlatformGovernance />);

    expect(
      await screen.findByText(/available only to organization owners/i),
    ).toBeInTheDocument();
    expect(mocks.quota).not.toHaveBeenCalled();
    expect(mocks.auditEvents).not.toHaveBeenCalled();
    expect(
      screen.getByRole("button", { name: "Refresh platform data" }),
    ).toBeDisabled();
  });
});
