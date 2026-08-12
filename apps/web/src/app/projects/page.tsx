import type { Metadata } from "next";

import { ProjectWorkbench } from "@/components/project-workbench";

export const metadata: Metadata = {
  title: "Project workspaces",
  description:
    "Create projects and import, inspect, and export immutable speech corpora.",
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
            file, then inspect its immutable initial version and verified
            exports.
          </p>
          <p className="honesty-note">
            This release creates and reads projects and version-1 corpora, and
            owners/admins can request retention-governed project deletion with
            exact confirmation. Project updates, individual corpus deletion, and
            later corpus versions are not yet available.
          </p>
        </div>
      </header>
      <ProjectWorkbench />
    </div>
  );
}
