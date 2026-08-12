import type { Metadata } from "next";

import { InventoryExplorer } from "@/components/inventory-explorer";

export const metadata: Metadata = {
  title: "Inventory Explorer",
  description:
    "Search and inspect source-explicit PHOIBLE inventories and eSpeak mappings.",
};

export default function InventoryPage() {
  return (
    <div className="workbench-page section-frame">
      <header className="workbench-intro">
        <div>
          <p className="eyebrow">PHOIBLE · All 38 features</p>
          <h1>Inventory Explorer</h1>
        </div>
        <p>
          Resolve best, source-specific, complete, or union inventories; inspect
          eSpeak mappings, marginal segments, allophones, feature contours, and
          exact dataset provenance.
        </p>
      </header>
      <InventoryExplorer />
    </div>
  );
}
