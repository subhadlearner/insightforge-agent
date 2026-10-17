"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { getWatchlist, listDiffs, type DiffRecord, type Finding, type WatchlistDashboard } from "@/lib/api";
import { findingLabel } from "@/lib/finding-label";

function findingText(f: Finding | null | undefined) {
  return f ? findingLabel(f) : "no corresponding Finding was produced";
}

function FindingMeta({ finding, label }: { finding: Finding | null | undefined; label: string }) {
  if (!finding) return null;
  return (
    <div className="diff__meta">
      <strong>{label} provenance:</strong>{" "}
      <span>
        {finding.derived_from
          ? `As of ${finding.as_of_period} (derived from source observation date)`
          : `As of ${finding.as_of_period}`}
      </span>
      {" · "}
      <Link href={`/reports/${encodeURIComponent(finding.provenance.report_id)}`}>
        Report
      </Link>
      {" · "}Evidence {finding.provenance.evidence_id}
      <details>
        <summary>Provenance</summary>
        <p>Run {finding.provenance.run_id}</p>
        <p>Claim: {finding.provenance.claim}</p>
        {finding.provenance.statement !== finding.provenance.claim && (
          <p>Evidence statement: {finding.provenance.statement}</p>
        )}
        <p>Sources: {finding.provenance.source_ids.join(", ") || "none"}</p>
        <p>
          Observations: {Object.entries(finding.provenance.observation_dates)
            .map(([id, date]) => `${id} at ${date}`).join("; ") || "none"}
        </p>
      </details>
    </div>
  );
}

export function WatchlistDiffView({ itemId }: { itemId: string }) {
  const [dashboard, setDashboard] = useState<WatchlistDashboard | null>(null);
  const [diffs, setDiffs] = useState<DiffRecord[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    let live = true;
    Promise.all([getWatchlist(itemId), listDiffs(itemId)])
      .then(([d, x]) => { if (live) { setDashboard(d); setDiffs(x); } })
      .catch((e: Error) => live && setError(e.message));
    return () => { live = false; };
  }, [itemId]);
  if (error) return <p className="page-status" role="alert">{error}</p>;
  if (!dashboard || !diffs) return <p className="page-status">Loading Diffs…</p>;
  return (
    <>
      <header className="diff-header">
        <h1 className="page__title">{dashboard.item.brief}</h1>
        <p>
          {dashboard.item.max_runs != null
            ? `${dashboard.runs_started} of ${dashboard.item.max_runs} runs started`
            : `${dashboard.runs_started} runs started`}
          {" · "}{dashboard.completed_runs} completed{" · "}{dashboard.failed_runs} failed
        </p>
      </header>
      {dashboard.unconfirmed_findings.length > 0 && (
        <section className="notice notice--quiet">
          <p className="notice__title">Unconfirmed Findings</p>
          <ul>{dashboard.unconfirmed_findings.map((x) => (
            <li key={x.finding.key}>{findingText(x.finding)} — not reconfirmed in {x.not_reconfirmed} successful Diffs.</li>
          ))}</ul>
        </section>
      )}
      {diffs.length === 0 ? <p>No later successful Watchlist Run has produced a Diff yet.</p> :
        diffs.map((diff) => (
          <article className="diff" key={diff.run_id}>
            <header>
              <h2>Run {diff.run_id}</h2>
              <p>Status: {diff.status}{diff.report_id && <> · <Link href={`/reports/${encodeURIComponent(diff.report_id)}`}>Report</Link></>}</p>
            </header>
            {diff.status === "failed" && <p className="notice notice--error">Diff failed: {diff.error}. Baseline was unchanged.</p>}
            {diff.intervening_failed.length > 0 && (
              <p className="notice notice--quiet">
                Net change is from the still-valid Baseline; {diff.intervening_failed.length} intervening Diff{diff.intervening_failed.length === 1 ? "" : "s"} failed.
              </p>
            )}
            {diff.skipped.length > 0 && (
              <section className="diff__skipped">
                <h3>Claims not normalized into Findings</h3>
                <p>These Claims are not Findings and are not changes.</p>
                <ul>{diff.skipped.map((skipped, index) => (
                  <li key={`${skipped.evidence_id}:${index}`}>
                    {skipped.claim} — {skipped.reason} (Evidence {skipped.evidence_id})
                  </li>
                ))}</ul>
              </section>
            )}
            <div className="diff__entries">
              {diff.entries.map(({ entry, material }, index) => (
                <section className={`diff-entry ${material ? "diff-entry--material" : ""}`} key={entry.key + index}>
                  <h3>{entry.outcome.replaceAll("_", " ")}{material ? " · material change" : ""}</h3>
                  {entry.previous && <p><strong>Previously established:</strong> {findingText(entry.previous)}</p>}
                  <p><strong>This Run:</strong> {findingText(entry.finding)}</p>
                  {entry.previous && <FindingMeta finding={entry.previous} label="Previously established" />}
                  {entry.finding && <FindingMeta finding={entry.finding} label="This Run" />}
                  {entry.unconfirmed && <p><strong>Unconfirmed:</strong> absence is not deletion; the established Finding remains.</p>}
                  {entry.reason && <p>{entry.reason}</p>}
                </section>
              ))}
            </div>
          </article>
        ))}
    </>
  );
}
