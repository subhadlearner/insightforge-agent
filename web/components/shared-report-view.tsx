"use client";

import { useEffect, useState } from "react";
import { getSharedReport, type PublicReport } from "@/lib/api";
import { formatDate } from "@/lib/format";
import { sectionLabel } from "@/lib/report";
import { ConfidenceLabel } from "./report-viewer";

export function SharedReportView({ token }: { token: string }) {
  const [report, setReport] = useState<PublicReport | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    let live = true;
    getSharedReport(token).then((x) => live && setReport(x))
      .catch((e: Error) => live && setError(e.message));
    return () => { live = false; };
  }, [token]);
  if (error) return <div className="page-status" role="alert"><h1>Shared Report unavailable</h1><p>{error}</p></div>;
  if (!report) return <p className="page-status">Loading shared Report…</p>;
  const fc = report.fact_check;
  return (
    <article className="report shared-report">
      <header className="report__header">
        <p className="report__meta">
          Shared Report of {formatDate(report.created_at) ?? report.created_at}
          {typeof fc.sampled === "number" && (
            <> · fact-checked {String(fc.sampled)} claims: {String(fc.verified ?? 0)} verified, {String(fc.unverified ?? 0)} unverified, {String(fc.unchecked ?? 0)} unchecked</>
          )}
        </p>
        <h1 className="report__title">{report.title}</h1>
        {report.brief && <p className="report__brief"><span className="report__brief-label">Brief</span> {report.brief}</p>}
      </header>
      {report.summary && (
        <section className="report__section" aria-labelledby="executive-summary">
          <h2 className="report__heading" id="executive-summary">Executive Summary</h2>
          <p className="report__summary">{report.summary}</p>
        </section>
      )}
      {report.sections.map((section, si) => (
        <section className="report__section" key={section.heading + si}>
          <h2 className="report__heading">{section.heading}</h2>
          <ConfidenceLabel confidence={sectionLabel(section.confidence)} />
          {section.claims.map((claim, ci) => (
            <p className={`claim${claim.historical ? " claim--historical" : ""}`} key={ci}>
              {claim.historical && <span className="claim__background">Background from an earlier Report{claim.historical_date ? ` of ${formatDate(claim.historical_date)}` : ""}: </span>}
              {claim.text}
            </p>
          ))}
        </section>
      ))}
      {report.gaps.length > 0 && <section className="report__section report__gaps"><h2 className="report__heading">Gaps &amp; limitations</h2><ul>{report.gaps.map((g) => <li key={g}>{g}</li>)}</ul></section>}
      <section className="report__section">
        <h2 className="report__heading">Web Sources</h2>
        {report.web_sources.length === 0 ? <p>No web Source metadata is available for this Report.</p> :
          <ul>{report.web_sources.map((s) => <li key={s.id}><a href={s.url} rel="noreferrer">{s.title}</a>{s.published_at ? ` · published ${formatDate(s.published_at)}` : ""}{s.credibility_score != null ? ` · credibility ${s.credibility_score}` : ""}</li>)}</ul>}
      </section>
    </article>
  );
}
