import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { GuidedDemo } from "@/components/guided-demo";

describe("GuidedDemo", () => {
  it("labels its values as illustrative and changes steps accessibly", async () => {
    const user = userEvent.setup();
    render(<GuidedDemo />);

    expect(
      screen.getByText(/not the output of a live CorpusGen run/i),
    ).toBeInTheDocument();
    const optimizeTab = screen.getByRole("tab", { name: /04 optimize/i });
    await user.click(optimizeTab);

    expect(optimizeTab).toHaveAttribute("aria-selected", "true");
    expect(screen.getByRole("tabpanel")).toHaveTextContent(
      "Keep fewer sentences with a reason",
    );
    expect(screen.getByText("Fixed preview data")).toBeInTheDocument();
  });
});
