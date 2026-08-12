"use client";

import { useEffect, useState } from "react";

import { describeRequestError } from "@/lib/api-client";
import {
  platformApi,
  type AuditEvent,
  type AuditPage,
  type QuotaSnapshot,
} from "@/lib/platform";
import { getCurrentPrincipal, type ProjectRole } from "@/lib/projects";

export function PlatformGovernance() {
  const [quota, setQuota] = useState<QuotaSnapshot | null>(null);
  const [audit, setAudit] = useState<AuditPage | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [role, setRole] = useState<ProjectRole | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    void getCurrentPrincipal(controller.signal)
      .then(async (principal) => {
        if (controller.signal.aborted) return;
        setRole(principal.role);
        if (principal.role !== "owner" && principal.role !== "admin") return;
        const [nextQuota, nextAudit] = await Promise.all([
          platformApi.quota(controller.signal),
          platformApi.auditEvents({ limit: 25 }, controller.signal),
        ]);
        if (!controller.signal.aborted) {
          setQuota(nextQuota);
          setAudit(nextAudit);
        }
      })
      .catch((caught: unknown) => {
        if (!controller.signal.aborted) setError(describeRequestError(caught));
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, []);

  async function refresh() {
    if (role !== "owner" && role !== "admin") return;
    setLoading(true);
    setError(null);
    try {
      const [nextQuota, nextAudit] = await Promise.all([
        platformApi.quota(),
        platformApi.auditEvents({ limit: 25 }),
      ]);
      setQuota(nextQuota);
      setAudit(nextAudit);
    } catch (caught) {
      setError(describeRequestError(caught));
    } finally {
      setLoading(false);
    }
  }

  async function loadNextPage() {
    if (!audit?.next_cursor || loadingMore) return;
    setLoadingMore(true);
    setError(null);
    try {
      const older = await platformApi.auditEvents({
        cursor: audit.next_cursor,
        limit: 25,
      });
      setAudit({
        events: [...audit.events, ...older.events],
        next_cursor: older.next_cursor,
      });
    } catch (caught) {
      setError(describeRequestError(caught));
    } finally {
      setLoadingMore(false);
    }
  }

  return (
    <section className="platform-governance" aria-busy={loading}>
      <div className="panel-heading">
        <div>
          <p className="eyebrow">Tenant controls</p>
          <h2>Quota & immutable audit</h2>
        </div>
        <button
          disabled={
            loading || (role !== null && role !== "owner" && role !== "admin")
          }
          onClick={() => void refresh()}
          type="button"
        >
          {loading ? "Refreshing…" : "Refresh platform data"}
        </button>
      </div>
      <p>
        Current usage is measured against server-owned policy. Audit rows are
        ordered, hash-linked records from the authenticated tenant.
      </p>
      {role && role !== "owner" && role !== "admin" ? (
        <p className="boundary-note" role="status">
          Tenant quota and audit evidence are available only to organization
          owners and administrators. Capability checks above remain visible to
          every signed-in role.
        </p>
      ) : null}
      {error ? (
        <div className="error-notice" role="alert">
          <strong>Platform data unavailable</strong>
          <p>{error}</p>
        </div>
      ) : null}
      {quota ? <QuotaPanel quota={quota} /> : null}
      {audit ? (
        <AuditPanel
          audit={audit}
          loadingMore={loadingMore}
          onLoadNextPage={loadNextPage}
        />
      ) : null}
      {!quota &&
      !audit &&
      !error &&
      (role === null || role === "owner" || role === "admin") ? (
        <p aria-live="polite" role="status">
          Loading tenant quota and audit events…
        </p>
      ) : null}
    </section>
  );
}

function QuotaPanel({ quota }: { readonly quota: QuotaSnapshot }) {
  const rows = [
    {
      label: "Active CPU jobs",
      value: quota.usage.active_cpu_jobs,
      maximum: quota.policy.max_concurrent_cpu_jobs,
      display: String(quota.usage.active_cpu_jobs),
    },
    {
      label: "Active expensive jobs",
      value: quota.usage.active_expensive_jobs,
      maximum: quota.policy.max_concurrent_expensive_jobs,
      display: String(quota.usage.active_expensive_jobs),
    },
    {
      label: "Artifact storage",
      value: quota.usage.artifact_bytes,
      maximum: quota.policy.max_artifact_bytes,
      display: formatBytes(quota.usage.artifact_bytes),
    },
    {
      label: "Artifact count",
      value: quota.usage.artifact_count,
      maximum: quota.policy.max_artifact_count,
      display: quota.usage.artifact_count.toLocaleString(),
    },
    {
      label: "Corpus sentences",
      value: quota.usage.corpus_sentences,
      maximum: quota.policy.max_corpus_sentences,
      display: quota.usage.corpus_sentences.toLocaleString(),
    },
  ] as const;
  return (
    <section className="workbench-panel" aria-labelledby="quota-heading">
      <h3 id="quota-heading">Current quota usage</h3>
      <ul className="quota-list">
        {rows.map((row) => (
          <li key={row.label}>
            <div>
              <strong>{row.label}</strong>
              <span>
                {row.display} / {formatLimit(row.label, row.maximum)}
              </span>
            </div>
            <meter
              aria-label={`${row.label} quota usage`}
              max={row.maximum}
              value={Math.min(row.value, row.maximum)}
            />
          </li>
        ))}
      </ul>
      <details>
        <summary>Run resource ceilings</summary>
        <dl className="inline-metadata">
          <PolicyValue
            label="Accepted sentences"
            value={quota.policy.max_generation_accepted_sentences}
          />
          <PolicyValue
            label="Generation iterations"
            value={quota.policy.max_generation_iterations}
          />
          <PolicyValue
            label="Activity deadline"
            value={`${quota.policy.max_activity_deadline_seconds}s`}
          />
          <PolicyValue
            label="Provider input tokens"
            value={quota.policy.max_provider_input_tokens}
          />
          <PolicyValue
            label="Provider output tokens"
            value={quota.policy.max_provider_output_tokens}
          />
          <PolicyValue
            label="Provider cost"
            value={`$${(quota.policy.max_provider_cost_microusd / 1_000_000).toFixed(2)}`}
          />
          <PolicyValue label="RL steps" value={quota.policy.max_rl_steps} />
          <PolicyValue label="RL tokens" value={quota.policy.max_rl_tokens} />
          <PolicyValue
            label="Checkpoint bytes"
            value={formatBytes(quota.policy.max_checkpoint_bytes)}
          />
        </dl>
      </details>
    </section>
  );
}

function AuditPanel({
  audit,
  loadingMore,
  onLoadNextPage,
}: {
  readonly audit: AuditPage;
  readonly loadingMore: boolean;
  readonly onLoadNextPage: () => Promise<void>;
}) {
  return (
    <section className="workbench-panel" aria-labelledby="audit-heading">
      <h3 id="audit-heading">Tenant audit events</h3>
      {!audit.events.length ? (
        <p className="workbench-empty">No audit events are visible yet.</p>
      ) : (
        <div
          aria-label="Immutable tenant audit events"
          className="table-scroller"
          role="region"
          tabIndex={0}
        >
          <table className="workbench-table">
            <thead>
              <tr>
                <th scope="col">Sequence</th>
                <th scope="col">Time</th>
                <th scope="col">Action</th>
                <th scope="col">Resource</th>
                <th scope="col">Actor</th>
                <th scope="col">Event hash</th>
              </tr>
            </thead>
            <tbody>
              {audit.events.map((event) => (
                <AuditRow event={event} key={event.sequence} />
              ))}
            </tbody>
          </table>
        </div>
      )}
      {audit.next_cursor ? (
        <button
          className="button"
          disabled={loadingMore}
          onClick={() => void onLoadNextPage()}
          type="button"
        >
          {loadingMore ? "Loading…" : "Load next audit page"}
        </button>
      ) : null}
    </section>
  );
}

function AuditRow({ event }: { readonly event: AuditEvent }) {
  return (
    <tr>
      <td>{event.sequence}</td>
      <td>{new Date(event.occurred_at).toLocaleString()}</td>
      <td>{event.action}</td>
      <td>
        {event.resource_type} <code>{event.resource_id.slice(0, 8)}</code>
      </td>
      <td>
        {event.actor_kind}: {event.actor_id}
      </td>
      <td>
        <abbr title={event.event_hash}>{event.event_hash.slice(0, 12)}…</abbr>
      </td>
    </tr>
  );
}

function PolicyValue({
  label,
  value,
}: {
  readonly label: string;
  readonly value: number | string;
}) {
  return (
    <div>
      <dt>{label}</dt>
      <dd>{typeof value === "number" ? value.toLocaleString() : value}</dd>
    </div>
  );
}

function formatLimit(label: string, value: number): string {
  return label === "Artifact storage"
    ? formatBytes(value)
    : value.toLocaleString();
}

function formatBytes(value: number): string {
  if (value < 1024) return `${value} B`;
  const units = ["KiB", "MiB", "GiB", "TiB"];
  let amount = value / 1024;
  let unit = units[0]!;
  for (const next of units.slice(1)) {
    if (amount < 1024) break;
    amount /= 1024;
    unit = next;
  }
  return `${amount.toFixed(amount >= 10 ? 1 : 2)} ${unit}`;
}
