import type { Metadata } from "next";

import { AdvancedWorkbench } from "@/components/advanced-workbench";

export const metadata: Metadata = {
  title: "Advanced Runtime Lab",
  description:
    "Validate and queue hosted, local, DATG, and Phon-RL work with bounded labs and CLI previews.",
};

export default function AdvancedPage() {
  return (
    <div className="workbench-page section-frame">
      <header className="workbench-intro">
        <div>
          <p className="eyebrow">Authorize · Estimate · Queue</p>
          <h1>Advanced Runtime Lab</h1>
        </div>
        <p>
          Explore CorpusGen model runtimes, Phon-DATG, Phon-RL, and CLI parity
          through a validation-only control plane. Heavy work is always a
          durable, profile-isolated worker job.
        </p>
      </header>
      <AdvancedWorkbench />
    </div>
  );
}
