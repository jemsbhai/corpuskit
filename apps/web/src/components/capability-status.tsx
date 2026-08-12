"use client";

import Link from "next/link";
import { useEffect, useState } from "react";

import {
  fetchCapabilityCatalog,
  getFallbackCatalog,
  controlsForCapability,
  type CapabilityCatalog,
  type CapabilityState,
} from "@/lib/capabilities";

const stateLabels: Record<CapabilityState, string> = {
  available: "Available",
  degraded: "Limited",
  unavailable: "Unavailable",
  planned: "Planned",
};

export function CapabilityStatus() {
  const [catalog, setCatalog] = useState<CapabilityCatalog | null>(null);
  const [refreshVersion, setRefreshVersion] = useState(0);
  const [refreshing, setRefreshing] = useState(false);

  useEffect(() => {
    const controller = new AbortController();
    void fetchCapabilityCatalog(controller.signal)
      .then(setCatalog)
      .catch((error: unknown) => {
        if (error instanceof DOMException && error.name === "AbortError")
          return;
        setCatalog(getFallbackCatalog());
      })
      .finally(() => {
        if (!controller.signal.aborted) setRefreshing(false);
      });
    return () => controller.abort();
  }, [refreshVersion]);

  if (catalog === null) {
    return (
      <div className="status-loading" aria-live="polite" role="status">
        <span className="status-spinner" aria-hidden="true" />
        Checking API-process capabilities...
      </div>
    );
  }

  const availableCount = catalog.capabilities.filter(
    (capability) => capability.status === "available",
  ).length;

  return (
    <div className="status-content">
      <div
        className={
          catalog.source === "fallback"
            ? "source-banner is-preview"
            : "source-banner"
        }
        role="status"
      >
        <div>
          <span className="source-kicker">
            {catalog.source === "api"
              ? "API process detection"
              : "Preview data"}
          </span>
          <strong>
            {catalog.source === "api"
              ? `${availableCount} of ${catalog.capabilities.length} checks available in the API process`
              : "The capability API is not connected"}
          </strong>
          <p>
            {catalog.source === "api"
              ? `Reported by ${catalog.environment}${catalog.engineVersion ? ` / CorpusGen ${catalog.engineVersion}` : ""}. This does not probe durable worker health.`
              : "The cards below are an illustrative roadmap, not claims about this deployment."}
          </p>
        </div>
        <div className="capability-refresh">
          <span className="source-indicator" aria-hidden="true" />
          <button
            className="button"
            disabled={refreshing}
            onClick={() => {
              setRefreshing(true);
              setRefreshVersion((value) => value + 1);
            }}
            type="button"
          >
            {refreshing ? "Refreshing…" : "Refresh capability checks"}
          </button>
        </div>
      </div>

      <ul
        className="capability-list"
        aria-label="API process capability checks"
      >
        {catalog.capabilities.map((capability) => (
          <li className="capability-card" key={capability.id}>
            <div className="capability-card-heading">
              <div>
                <span className={`status-pill status-${capability.status}`}>
                  <i aria-hidden="true" /> {stateLabels[capability.status]}
                </span>
                <h2>{capability.name}</h2>
              </div>
              <span className="worker-profile">
                <span className="sr-only">Execution profile: </span>
                {capability.profile}
                {capability.required ? " / Required" : ""}
              </span>
            </div>
            <p>{capability.description}</p>
            {capability.version ? (
              <p>
                <strong>Reported version:</strong> {capability.version}
              </p>
            ) : null}
            {capability.reason ? (
              <div className="remediation">
                <strong>Exact remediation</strong>
                <p>{capability.reason}</p>
              </div>
            ) : null}
            <div className="affected-controls">
              <strong>Affected controls</strong>
              {controlsForCapability(capability.id).length ? (
                <ul>
                  {controlsForCapability(capability.id).map((control) => (
                    <li
                      key={`${capability.id}-${control.href}-${control.label}`}
                    >
                      <Link href={control.href} prefetch={false}>
                        {control.label}
                      </Link>
                      <span>{control.requirement}</span>
                    </li>
                  ))}
                </ul>
              ) : (
                <p>No browser control is mapped to this check.</p>
              )}
            </div>
          </li>
        ))}
      </ul>

      <p className="status-footnote">
        Availability describes dependency and data detection inside this API
        process only. It does not establish that a Temporal worker is running,
        healthy, or configured. Each run still validates language, data, model,
        quota, and worker requirements.
      </p>
    </div>
  );
}
