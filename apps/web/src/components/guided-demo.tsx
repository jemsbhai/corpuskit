"use client";

import Link from "next/link";
import { useState } from "react";

const steps = [
  {
    id: "import",
    number: "01",
    label: "Import",
    title: "A small, inspectable starting point",
    body: "The illustrative Riverbend corpus contains 12 short English prompts. The Project Workspace imports bounded TXT, CSV, JSON, or manual input into an immutable normalized version with a content hash.",
    href: "/projects",
    action: "Open Project Workspace",
    metric: "12",
    unit: "sentences",
    secondary: "96 unique words",
    bars: [30, 46, 35, 59, 41, 52, 37, 65, 45, 56, 39, 49],
  },
  {
    id: "inventory",
    number: "02",
    label: "Inventory",
    title: "Know what coverage means",
    body: "Inventory Explorer presents the provisioned PHOIBLE revision and digest alongside language mappings, segment classes, marginal phonemes, allophones, and distinctive features.",
    href: "/inventory",
    action: "Open Inventory Explorer",
    metric: "38",
    unit: "target units",
    secondary: "PHOIBLE mapped · demo value",
    bars: [62, 55, 73, 49, 81, 68, 57, 77, 45, 71, 66, 52],
  },
  {
    id: "evaluate",
    number: "03",
    label: "Evaluate",
    title: "See the gaps, not just a score",
    body: "Evaluation Studio sends your rows to the live API and returns counts, distribution quality, missing units, and source-level contribution evidence. Only the fixed values shown here are illustrative.",
    href: "/evaluate",
    action: "Open Evaluation Studio",
    metric: "84%",
    unit: "coverage",
    secondary: "6 units remain · illustrative",
    bars: [78, 84, 69, 91, 73, 87, 94, 66, 82, 88, 76, 91],
  },
  {
    id: "optimize",
    number: "04",
    label: "Optimize",
    title: "Keep fewer sentences with a reason",
    body: "Selection Studio runs all six supported algorithms against the same candidate and target contract, then compares their returned coverage, size, missing units, runtime, and metadata without changing the source corpus.",
    href: "/selection",
    action: "Open Selection Studio",
    metric: "7",
    unit: "selected",
    secondary: "of 12 sentences · illustrative",
    bars: [96, 91, 86, 79, 73, 66, 58, 48, 38, 28, 19, 12],
  },
] as const;

export function GuidedDemo() {
  const [activeId, setActiveId] = useState<(typeof steps)[number]["id"]>(
    steps[0].id,
  );
  const active = steps.find((step) => step.id === activeId) ?? steps[0];

  return (
    <section
      className="guided-demo"
      id="guided-demo"
      aria-labelledby="demo-title"
    >
      <div className="section-frame">
        <div className="demo-heading">
          <div>
            <p className="eyebrow">Illustrative walkthrough</p>
            <h2 id="demo-title">A corpus decision in four clear moves.</h2>
          </div>
          <p>
            Learn the workflow with fixed sample values, then open the linked
            workbench for live API results. These preview values are
            illustrative, not the output of a live CorpusGen run.
          </p>
        </div>

        <div className="demo-workbench">
          <div
            className="demo-tabs"
            role="tablist"
            aria-label="Guided demo steps"
          >
            {steps.map((step) => (
              <button
                aria-controls={`panel-${step.id}`}
                aria-selected={active.id === step.id}
                className={
                  active.id === step.id ? "demo-tab is-active" : "demo-tab"
                }
                id={`tab-${step.id}`}
                key={step.id}
                onClick={() => setActiveId(step.id)}
                role="tab"
                tabIndex={active.id === step.id ? 0 : -1}
                type="button"
              >
                <span>{step.number}</span>
                {step.label}
              </button>
            ))}
          </div>

          <div
            aria-labelledby={`tab-${active.id}`}
            className="demo-panel"
            id={`panel-${active.id}`}
            role="tabpanel"
          >
            <div className="demo-explanation">
              <p className="eyebrow">Step {active.number}</p>
              <h3>{active.title}</h3>
              <p>{active.body}</p>
              <span className="demo-disclosure">Fixed preview data</span>
              <Link className="text-link" href={active.href}>
                {active.action} <span aria-hidden="true">↗</span>
              </Link>
            </div>
            <div className="demo-metric-card">
              <p>Riverbend corpus</p>
              <strong>{active.metric}</strong>
              <span>{active.unit}</span>
              <div className="micro-bars" aria-hidden="true">
                {active.bars.map((height, index) => (
                  <i
                    key={`${active.id}-${index}`}
                    style={{ height: `${height}%` }}
                  />
                ))}
              </div>
              <small>{active.secondary}</small>
            </div>
          </div>
        </div>
      </div>
    </section>
  );
}
