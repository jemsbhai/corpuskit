import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { SiteHeader } from "@/components/site-header";

vi.mock("next/navigation", () => ({
  usePathname: () => "/",
}));

describe("SiteHeader", () => {
  it("exposes current navigation and an operable mobile menu", async () => {
    const user = userEvent.setup();
    render(<SiteHeader />);

    expect(screen.getByRole("link", { name: "Home" })).toHaveAttribute(
      "aria-current",
      "page",
    );
    expect(screen.getByRole("link", { name: "Status" })).not.toHaveAttribute(
      "aria-current",
    );

    const toggle = screen.getByRole("button", { name: "Toggle navigation" });
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    await user.click(toggle);
    expect(toggle).toHaveAttribute("aria-expanded", "true");
    expect(
      screen.getByRole("navigation", { name: "Primary navigation" }),
    ).toHaveClass("is-open");

    const capabilityLink = screen.getByRole("link", {
      name: "Status",
    });
    capabilityLink.addEventListener("click", (event) => event.preventDefault());
    await user.click(capabilityLink);
    expect(toggle).toHaveAttribute("aria-expanded", "false");
  });
});
