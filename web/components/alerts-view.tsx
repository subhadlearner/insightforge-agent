"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { listAlerts, markAlertRead, type Alert } from "@/lib/api";
import { findingLabel } from "@/lib/finding-label";

export function AlertsView() {
  const [alerts, setAlerts] = useState<Alert[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    let live = true;
    listAlerts().then((x) => live && setAlerts(x)).catch((e: Error) => live && setError(e.message));
    return () => { live = false; };
  }, []);
  async function toggle(alert: Alert) {
    try {
      const next = await markAlertRead(alert.id, !alert.read);
      setAlerts((old) => old?.map((a) => a.id === next.id ? next : a) ?? null);
    } catch (e) { setError((e as Error).message); }
  }
  if (error) return <p className="page-status" role="alert">{error}</p>;
  if (!alerts) return <p className="page-status">Loading Alerts…</p>;
  if (!alerts.length) return <p className="page-status">No material-change Alerts yet.</p>;
  return (
    <div className="alerts">
      {alerts.map((alert) => (
        <article className={`alert-card ${alert.read ? "" : "alert-card--unread"}`} key={alert.id}>
          <header>
            <h2>{alert.brief}</h2>
            <p><Link href={`/reports/${encodeURIComponent(alert.report_id)}`}>Open new Report</Link></p>
          </header>
          <ul>
            {alert.changes.map((c, i) => (
              <li key={c.evidence_id ?? i}>
                <strong>{c.outcome.replaceAll("_", " ").replace(/^./, (letter) => letter.toUpperCase())}</strong>
                {c.previous_object != null && c.previous_polarity && (
                  <div>Previously established — {findingLabel({ subject: c.subject, predicate: c.predicate, object: c.previous_object, polarity: c.previous_polarity, scope: c.scope })}</div>
                )}
                <div>This Run — {findingLabel(c)}</div>
                <div>{c.when}</div>
                {c.previous_report_id && <> <Link href={`/reports/${encodeURIComponent(c.previous_report_id)}`}>Previous establishing Report</Link>.</>}
              </li>
            ))}
          </ul>
          {(alert.failed_diffs ?? []).length > 0 && (
            <p>There were {(alert.failed_diffs ?? []).length} intervening failed Diffs; this Alert reflects the net change from the still-valid Baseline.</p>
          )}
          <button className="link-button" type="button" onClick={() => toggle(alert)}>
            Mark {alert.read ? "unread" : "read"}
          </button>
        </article>
      ))}
    </div>
  );
}
