import type { Metadata } from "next";

import { ProjectWorkbench } from "@/components/project-workbench";

export const metadata: Metadata = {
  title: "Project workspaces",
  description:
    "Create projects and build, inspect, and export immutable speech corpus histories.",
};

export default function ProjectsPage() {
  return (
    <div className="projects-page section-frame">
      <header className="projects-intro">
        <div>
          <p className="eyebrow">
            <span>Corpus operations</span> Tenant scoped
          </p>
          <h1>Project workspaces</h1>
        </div>
        <div>
          <p>
            Build a reproducible corpus from manual sentences or a bounded UTF-8
            file, add immutable successor versions, then inspect any snapshot
            and its verified exports.
          </p>
          <p className="honesty-note">
            Owners, admins, and editors can create corpora and later immutable
            versions. Owners/admins can request retention-governed project
            deletion with exact confirmation. Project updates and individual
            corpus deletion are not yet available.
          </p>
        </div>
      </header>
      <ProjectWorkbench />
    </div>
  );
}
