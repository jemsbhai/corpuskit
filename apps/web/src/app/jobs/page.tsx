import type { Metadata } from "next";

import { JobCenter } from "@/components/job-center";

export const metadata: Metadata = {
  title: "Job Center",
  description:
    "Submit and monitor typed, durable CorpusKit runs for the selected project.",
};

export default function JobsPage() {
  return (
    <div className="workbench-page section-frame">
      <header className="workbench-intro">
        <div>
          <p className="eyebrow">Submit · Monitor · Recover</p>
          <h1>Job Center</h1>
        </div>
        <p>
          Build one of six registered CPU run specifications, follow its
          monotonic event stream, and manage cancellation, retry, and final
          artifacts.
        </p>
      </header>
      <JobCenter />
    </div>
  );
}
