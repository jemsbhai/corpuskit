import type { Metadata } from "next";

import { ArtifactManager } from "@/components/artifact-manager";

export const metadata: Metadata = {
  title: "Artifact Manager",
  description:
    "Integrity-aware artifact management for the selected CorpusKit project.",
};

export default function ArtifactsPage() {
  return (
    <div className="workbench-page section-frame">
      <header className="workbench-intro">
        <div>
          <p className="eyebrow">Upload · Verify · Retain</p>
          <h1>Artifact Manager</h1>
        </div>
        <p>
          Manage immutable corpus and run-output artifacts within the selected
          project, with full-object integrity checks and explicit destructive
          confirmation.
        </p>
      </header>
      <ArtifactManager />
    </div>
  );
}
