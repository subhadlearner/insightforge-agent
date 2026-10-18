"use client";

// The Report as a reader sees it: the fact-checked text (lib/report.ts reads
// body.verified.draft), one numbered marker per Claim that opens its source, the
// [UNVERIFIED] mark kept as written, and Historical Claims set apart as background. Each
// section shows its Confidence under its heading, and each Claim its Source inline as
// [Source: domain, date, credibility: score] once citations load (the drawer has the rest).

import { useRef, useState, type ReactNode } from "react";
import type { Citation } from "@/lib/api";
import { formatDate } from "@/lib/format";
import {
  attachCitations, inlineSource, UNVERIFIED_MARK,
  type ParsedClaim, type ParsedReport, type SectionConfidence,
} from "@/lib/report";
import { CitationDrawer } from "./citation-drawer";

export type CitationsState =
  | { kind: "loading" }
  | { kind: "ready"; citations: Citation[] }
  | { kind: "failed"; status: number; message: string };

function CitationsNotice({ state }: { state: CitationsState }) {
  if (state.kind !== "failed") return null;
  return (
    <div className="notice notice--error report__notice" role="alert" data-testid="citations-unavailable">
      <p className="notice__title">Sources unavailable</p>
      <p>
        {state.status === 500
          ? "Supporting evidence is unavailable because this Report's stored citation provenance could not be resolved."
          : `Supporting evidence could not be loaded: ${state.message}`}
      </p>
    </div>
  );
}

/** The label under a section heading. Historical background has no current Confidence, so
 *  it is labelled as background rather than given one; an unresolved label says so. */
export function ConfidenceLabel({ confidence }: { confidence: SectionConfidence }) {
  if (confidence === null) return null;
  if (confidence === "unresolved") {
    return (
      <p className="confidence confidence--unresolved" data-testid="section-confidence">
        Confidence unavailable
      </p>
    );
  }
  if (confidence === "background") {
    return (
      <p className="confidence confidence--background" data-testid="section-confidence">
        Background from earlier Reports · no current Confidence
      </p>
    );
  }
  return (
    <p className={`confidence confidence--${confidence.toLowerCase()}`} data-testid="section-confidence">
      Confidence: <strong>{confidence}</strong>
    </p>
  );
}

/** The Claim's words, its [UNVERIFIED] mark as written, and its marker. The last word is
 *  kept on one line with the mark and marker, so the marker never wraps on its own. */
function ClaimText({ claim, marker }: { claim: ParsedClaim; marker: ReactNode }) {
  const split = claim.statement.lastIndexOf(" ");
  const head = split > 0 ? claim.statement.slice(0, split + 1) : "";
  const tail = split > 0 ? claim.statement.slice(split + 1) : claim.statement;
  return (
    <>
      <span className="claim__text">{head}</span>
      <span className="claim__tail">
        <span className="claim__text">{tail}</span>
        {claim.unverified && (
          <span className="claim__unverified" title="The Fact-Checker could not confirm this claim against its source.">
            {UNVERIFIED_MARK.trim()}
          </span>
        )}
        {marker}
      </span>
    </>
  );
}

export function ReportViewer({ report, citations }: { report: ParsedReport; citations: CitationsState }) {
  const byClaim = citations.kind === "ready" ? attachCitations(report, citations.citations) : new Map<string, Citation>();
  const claims = report.sections.flatMap((s) => s.claims);
  const [openKey, setOpenKey] = useState<string | null>(null);
  const markers = useRef(new Map<string, HTMLButtonElement>());
  const openClaim = claims.find((c) => c.key === openKey) ?? null;

  function close() {
    const key = openKey;
    setOpenKey(null);
    if (key) markers.current.get(key)?.focus(); // back to the marker that opened it
  }

  function step(delta: -1 | 1) {
    if (!openClaim) return;
    const next = claims[openClaim.number - 1 + delta];
    if (next) setOpenKey(next.key);
  }

  function marker(claim: ParsedClaim) {
    const available = byClaim.has(claim.key);
    const label = citations.kind === "loading"
      ? `Loading source for claim ${claim.number}`
      : available ? `View source for claim ${claim.number}` : `Source unavailable for claim ${claim.number}`;
    return (
      <button
        type="button"
        className="cite"
        ref={(el) => {
          if (el) markers.current.set(claim.key, el);
          else markers.current.delete(claim.key);
        }}
        aria-label={label}
        aria-haspopup="dialog"
        disabled={!available}
        onClick={() => setOpenKey(claim.key)}
      >
        {claim.number}
      </button>
    );
  }

  return (
    <article className="report">
      <header className="report__header">
        <p className="report__meta">
          Report of {formatDate(report.createdAt) ?? report.createdAt}
          {report.factCheck && report.factCheck.sampled > 0 && (
            <> <span aria-hidden="true">/</span> fact-checked {report.factCheck.sampled} claims: {report.factCheck.verified} verified, {report.factCheck.unverified} unverified, {report.factCheck.unchecked} unchecked</>
          )}
        </p>
        <h1 className="report__title">{report.title}</h1>
        {report.brief && <p className="report__brief"><span className="report__brief-label">Brief</span> {report.brief}</p>}
      </header>

      <CitationsNotice state={citations} />

      {report.summary && (
        <section className="report__section" aria-labelledby="executive-summary">
          <h2 className="report__heading" id="executive-summary">Executive Summary</h2>
          <p className="report__summary">{report.summary}</p>
        </section>
      )}

      {report.sections.map((section) => (
        <section key={section.heading + section.claims[0]?.key} className="report__section">
          <h2 className="report__heading">{section.heading}</h2>
          <ConfidenceLabel confidence={section.confidence} />
          {section.claims.length === 0 && <p className="report__empty">No claims in this section.</p>}
          {section.claims.map((claim) => (
            <p key={claim.key} className={`claim${claim.historical ? " claim--historical" : ""}${claim.unverified ? " claim--unverified" : ""}`} data-claim={claim.key}>
              {claim.historical && (
                <span className="claim__background">
                  Background from an earlier Report{claim.historicalDate ? ` of ${formatDate(claim.historicalDate)}` : ""}:{" "}
                </span>
              )}
              <ClaimText claim={claim} marker={marker(claim)} />
              {byClaim.has(claim.key) && (
                <span className="claim__source"> {inlineSource(byClaim.get(claim.key)!)}</span>
              )}
            </p>
          ))}
        </section>
      ))}

      {report.gaps.length > 0 && (
        <section className="report__section report__gaps">
          <h2 className="report__heading">Gaps &amp; limitations</h2>
          <ul>
            {report.gaps.map((gap) => <li key={gap}>{gap}</li>)}
          </ul>
        </section>
      )}

      <section className="report__print-sources" aria-label="Print sources"
        data-print-sources-state={citations.kind}>
          <h2>Sources</h2>
          {citations.kind === "failed" ? (
            <p>Sources unavailable because this Report's stored citation provenance could not be resolved.</p>
          ) : citations.kind === "loading" ? (
            <p>Loading Sources…</p>
          ) : <ol>
            {claims.map((claim) => {
              const source = byClaim.get(claim.key);
              return (
                <li key={claim.key} value={claim.number}>
                  <strong>Source {claim.number}:</strong>{" "}
                  {source ? (
                    <>
                      {source.source_title}
                      {" · Published: "}{formatDate(source.published_at) ?? "Not stated"}
                      {" · Credibility: "}{source.credibility_score != null ? `${source.credibility_score.toFixed(2)} of 1` : "Not scored"}
                      {source.page != null && <>{" · Page "}{source.page}</>}
                      {source.section_heading && <>{" · Section: "}{source.section_heading}</>}
                    </>
                  ) : "Source unavailable"}
                </li>
              );
            })}
          </ol>}
        </section>

      <CitationDrawer
        claim={openClaim}
        citation={openClaim ? byClaim.get(openClaim.key) ?? null : null}
        total={claims.length}
        onClose={close}
        onStep={step}
      />
    </article>
  );
}
