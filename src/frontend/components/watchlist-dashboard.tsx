"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { listWatchlists, type WatchlistDashboard as Dashboard } from "@/lib/api";
import { findingLabel } from "@/lib/finding-label";

export function WatchlistDashboard() {
  const [items, setItems] = useState<Dashboard[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    let live = true;
    listWatchlists().then((x) => live && setItems(x))
      .catch((e: Error) => live && setError(e.message));
    return () => { live = false; };
  }, []);
  if (error) return <p className="page-status" role="alert">{error}</p>;
  if (!items) return <p className="page-status" aria-live="polite">Loading Watchlist…</p>;
  if (!items.length) return <p className="page-status">No Watchlist Items yet.</p>;
  return (
    <div className="watchlist-grid">
      {items.map((d) => {
        const details = new Map(d.recent_failures.map((failure) => [failure.run_id, failure]));
        return (
        <article className="watchlist-card" key={d.item.id}>
          <h2>{d.item.brief}</h2>
          <p className="watchlist-card__counts">
            {d.item.max_runs != null
              ? `${d.runs_started} of ${d.item.max_runs} runs started`
              : `${d.runs_started} runs started`}
            {" · "}{d.completed_runs} completed{" · "}{d.failed_runs} failed
          </p>
          <p>Every {d.item.cadence_minutes} minute{d.item.cadence_minutes === 1 ? "" : "s"}.</p>
          {d.recent_failed_runs.length > 0 && (
            <div>
              <h3>Recent Failed Runs</h3>
              <ul>{d.recent_failed_runs.map((run) => {
                const failure = details.get(run.id);
                return (
                  <li key={run.id}>
                    {failure
                      ? `${failure.stage ? `${failure.stage}: ` : ""}${failure.reason}`
                      : `Run ${run.id} failed`}
                  </li>
                );
              })}</ul>
            </div>
          )}
          {d.unconfirmed_findings.length > 0 && (
            <div>
              <h3>Unconfirmed Findings</h3>
              <ul>{d.unconfirmed_findings.map((x) => (
                <li key={x.finding.key}>
                  {findingLabel(x.finding)}
                  {" "}({x.not_reconfirmed} runs without reconfirmation)
                </li>
              ))}</ul>
            </div>
          )}
          <Link href={`/watchlist/${encodeURIComponent(d.item.id)}`}>View Diff history</Link>
        </article>
        );
      })}
    </div>
  );
}
