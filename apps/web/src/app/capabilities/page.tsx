import type { Metadata } from "next";

import { CapabilityStatus } from "@/components/capability-status";
import { PlatformGovernance } from "@/components/platform-governance";

export const metadata: Metadata = {
  title: "Capability status",
  description: "Connected CorpusKit API-process dependency detection.",
};

export default function CapabilitiesPage() {
  return (
    <section
      className="section-frame status-page"
      aria-labelledby="capability-title"
    >
      <div className="status-intro">
        <p className="eyebrow">System transparency</p>
        <h1 id="capability-title">Capability status</h1>
        <p>
          See which optional dependencies the connected API process can detect
          and which controls they gate. This is not worker health or proof that
          a durable execution profile is ready. Status comes from{" "}
          <code>/api/v1/capabilities</code> when connected.
        </p>
        <p>
          Each live check is mapped to the controls it gates and preserves the
          backend&apos;s exact remediation text. An installed dependency does
          not imply that an unregistered route or worker can execute it.
        </p>
      </div>
      <CapabilityStatus />
      <PlatformGovernance />
    </section>
  );
}
