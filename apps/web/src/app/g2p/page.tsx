import type { Metadata } from "next";

import { G2PStudio } from "@/components/g2p-studio";

export const metadata: Metadata = {
  title: "G2P Studio",
  description:
    "Transcribe single or ordered batches through the connected eSpeak backend.",
};

export default function G2PPage() {
  return (
    <div className="workbench-page section-frame">
      <header className="workbench-intro">
        <div>
          <p className="eyebrow">Grapheme to phoneme · Synchronous</p>
          <h1>G2P Studio</h1>
        </div>
        <p>
          Convert multilingual text to normalized IPA, phonemes, diphones, and
          triphones while preserving batch order. Results always come from the
          connected backend.
        </p>
      </header>
      <G2PStudio />
    </div>
  );
}
