import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("@/lib/browser-auth", () => ({
  authenticatedFetch: (input: RequestInfo | URL, init?: RequestInit) =>
    fetch(input, init),
}));

import { EvaluationStudio } from "@/components/evaluation-studio";
import { validEvaluationResult } from "@/lib/evaluation.test";

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("EvaluationStudio", () => {
  it("submits text and renders validated coverage provenance", async () => {
    const user = userEvent.setup();
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify(validEvaluationResult), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );
    vi.stubGlobal("fetch", fetchMock);
    vi.stubGlobal("requestAnimationFrame", (callback: FrameRequestCallback) => {
      callback(0);
      return 1;
    });
    render(<EvaluationStudio />);

    await user.type(
      screen.getByRole("textbox", { name: /sentences/i }),
      "Hello.",
    );
    await user.click(screen.getByRole("radio", { name: /explicit IPA list/i }));
    await user.type(
      screen.getByRole("textbox", { name: /target phonemes/i }),
      "h, ə, l, oʊ, z",
    );
    await user.click(screen.getByRole("button", { name: "Evaluate corpus" }));

    expect(
      await screen.findByRole("heading", { name: "80.0% coverage" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("list", { name: "Missing target units" }),
    ).toHaveTextContent("z");
    expect(screen.getByRole("table")).toHaveTextContent("Hello.");
    expect(screen.getByRole("table")).toHaveTextContent("h · ə · l · oʊ");
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("sends non-default language, unit, and PHOIBLE configuration", async () => {
    const user = userEvent.setup();
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({
          ...validEvaluationResult,
          language: "fr-fr",
          unit: "diphone",
          target_mode: "phoible",
        }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      ),
    );
    vi.stubGlobal("fetch", fetchMock);
    vi.stubGlobal("requestAnimationFrame", (callback: FrameRequestCallback) => {
      callback(0);
      return 1;
    });
    render(<EvaluationStudio />);

    await user.type(
      screen.getByRole("textbox", { name: /sentences/i }),
      "Bonjour.",
    );
    await user.clear(screen.getByRole("combobox", { name: "Language code" }));
    await user.type(
      screen.getByRole("combobox", { name: "Language code" }),
      "fr-fr",
    );
    await user.click(screen.getByRole("radio", { name: "diphone" }));
    await user.click(screen.getByRole("radio", { name: /PHOIBLE inventory/i }));
    await user.click(screen.getByRole("button", { name: "Evaluate corpus" }));

    await screen.findByRole("heading", { name: "80.0% coverage" });
    const request = fetchMock.mock.calls[0]?.[1] as RequestInit;
    expect(JSON.parse(request.body as string)).toMatchObject({
      language: "fr-fr",
      unit: "diphone",
      target: { mode: "phoible", phonemes: [] },
    });
  });

  it("renders complete coverage and absent optional distribution honestly", async () => {
    const user = userEvent.setup();
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(
          JSON.stringify({
            ...validEvaluationResult,
            covered_units: validEvaluationResult.target_units,
            missing_units: [],
            coverage: 1,
            distribution: null,
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        ),
      ),
    );
    vi.stubGlobal("requestAnimationFrame", (callback: FrameRequestCallback) => {
      callback(0);
      return 1;
    });
    render(<EvaluationStudio />);

    await user.type(
      screen.getByRole("textbox", { name: /sentences/i }),
      "Hello.",
    );
    await user.click(screen.getByRole("button", { name: "Evaluate corpus" }));

    expect(
      await screen.findByText("Every target unit is covered."),
    ).toBeInTheDocument();
    expect(
      screen.getByText("Distribution metrics were not returned for this run."),
    ).toBeInTheDocument();
  });

  it("shows a loading state and supports cancellation", async () => {
    const user = userEvent.setup();
    vi.stubGlobal(
      "fetch",
      vi.fn(
        (_url: string, init: RequestInit) =>
          new Promise((_resolve, reject) => {
            init.signal?.addEventListener("abort", () =>
              reject(new DOMException("Aborted", "AbortError")),
            );
          }),
      ),
    );
    render(<EvaluationStudio />);

    await user.type(
      screen.getByRole("textbox", { name: /sentences/i }),
      "Hello.",
    );
    await user.click(screen.getByRole("button", { name: "Evaluate corpus" }));
    expect(screen.getByRole("status", { name: "" })).toHaveTextContent(
      "Running a live evaluation",
    );
    expect(screen.getByRole("button", { name: "Evaluating…" })).toBeDisabled();

    await user.click(screen.getByRole("button", { name: "Cancel" }));
    expect(
      await screen.findByText("Evaluation cancelled. No result was computed."),
    ).toBeInTheDocument();
  });

  it("rejects empty and incomplete explicit input before transport", async () => {
    const user = userEvent.setup();
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    render(<EvaluationStudio />);

    await user.click(screen.getByRole("button", { name: "Evaluate corpus" }));
    expect(screen.getByRole("alert")).toHaveTextContent(
      "Add at least one non-empty sentence",
    );

    await user.type(
      screen.getByRole("textbox", { name: /sentences/i }),
      "Hello.",
    );
    await user.click(screen.getByRole("radio", { name: /explicit IPA list/i }));
    await user.click(screen.getByRole("button", { name: "Evaluate corpus" }));
    expect(screen.getByRole("alert")).toHaveTextContent(
      "Add at least one target phoneme",
    );
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("never substitutes preview results when transport fails", async () => {
    const user = userEvent.setup();
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new TypeError("offline")));
    render(<EvaluationStudio />);

    await user.click(
      screen.getByRole("button", { name: "Load preview input" }),
    );
    expect(screen.getByRole("status")).toHaveTextContent(
      "Submit it to request a real API evaluation",
    );
    expect(screen.queryByText(/% coverage/)).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Evaluate corpus" }));
    await waitFor(() =>
      expect(screen.getByRole("alert")).toHaveTextContent(
        "No result was computed",
      ),
    );
    expect(screen.queryByText(/% coverage/)).not.toBeInTheDocument();
  });

  it("imports a bounded plain-text file without claiming evaluation", async () => {
    const user = userEvent.setup();
    render(<EvaluationStudio />);
    const file = new File(["First sentence.\nSecond sentence."], "sample.txt", {
      type: "text/plain",
    });

    await user.upload(screen.getByLabelText("Import .txt"), file);

    expect(
      await screen.findByText(
        /loaded into the form. It has not been evaluated yet/i,
      ),
    ).toBeInTheDocument();
    expect(screen.getByRole("textbox", { name: /sentences/i })).toHaveValue(
      "First sentence.\nSecond sentence.",
    );
  });
});
