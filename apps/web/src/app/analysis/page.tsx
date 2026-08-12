import type { Metadata } from "next";

import { InventoryAnalysisWorkbench } from "@/components/inventory-analysis-workbench";

export const metadata: Metadata = {
  title: "Inventory & Analysis",
  description:
    "Browse provisioned PHOIBLE inventories and run CorpusGen corpus analyses.",
};

export default function AnalysisPage() {
  return (
    <div className="analysis-page">
      <section
        className="section-frame analysis-intro"
        aria-labelledby="analysis-title"
      >
        <div>
          <p className="eyebrow">Live tools · Inspect before you optimize</p>
          <h1 id="analysis-title">Inventory &amp; Analysis</h1>
        </div>
        <p>
          Find eSpeak language mappings, inspect locally provisioned PHOIBLE
          segments, and run focused corpus diagnostics. Every result shown here
          comes from the connected CorpusKit API.
        </p>
      </section>
      <div className="section-frame">
        <InventoryAnalysisWorkbench />
      </div>
    </div>
  );
}
