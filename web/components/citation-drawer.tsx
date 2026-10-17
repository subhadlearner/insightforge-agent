"use client";

// The evidence behind one Claim, as GET /api/reports/{id}/citations resolved it: the Claim's
// own primary Passage and that Passage's Source. Nothing here is derived in the browser.
// A native <dialog>: Escape closes it, focus moves into it and returns to the marker.

import { useEffect, useRef } from "react";
import type { Citation } from "@/lib/api";
import { formatDate } from "@/lib/format";
import type { ParsedClaim } from "@/lib/report";

export function CitationDrawer({ claim, citation, total, onClose, onStep }: {
  claim: ParsedClaim | null;
  citation: Citation | null;
  total: number;
  onClose: () => void;
  onStep: (delta: -1 | 1) => void;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  const open = claim !== null;

  useEffect(() => {
    const d = dialog.current;
    if (!d) return;
    if (open && !d.open) d.showModal();
    if (!open && d.open) d.close();
  }, [open]);

  const published = formatDate(citation?.published_at);
  const score = citation?.credibility_score;

  return (
    <dialog
      ref={dialog}
      className="drawer"
      aria-labelledby="drawer-title"
      onClose={onClose}
      onClick={(e) => {
        if (e.target === dialog.current) onClose(); // a click on the backdrop
      }}
    >
      {claim && (
        <div className="drawer__inner">
          <header className="drawer__header">
            <h2 id="drawer-title" className="drawer__title">Source for claim {claim.number}</h2>
            <button type="button" className="drawer__close" onClick={onClose} aria-label="Close source">Close</button>
          </header>

          <blockquote className="drawer__claim">{claim.text}</blockquote>

          {citation ? (
            <>
              <dl className="drawer__meta">
                <div>
                  <dt>Source</dt>
                  <dd className="drawer__source" data-testid="citation-source">{citation.source_title}</dd>
                </div>
                <div>
                  <dt>Published</dt>
                  <dd data-testid="citation-published">{published ?? "Not stated by the source"}</dd>
                </div>
                <div>
                  <dt>Credibility</dt>
                  <dd data-testid="citation-credibility">
                    {typeof score === "number" ? (
                      <>
                        <meter className="drawer__meter" min={0} max={1} low={0.4} high={0.65} optimum={1} value={score} />{" "}
                        {score.toFixed(2)} of 1
                      </>
                    ) : "Not scored"}
                  </dd>
                </div>
                {citation.page != null && (
                  <div>
                    <dt>Page</dt>
                    <dd>{citation.page}</dd>
                  </div>
                )}
                {citation.section_heading && (
                  <div>
                    <dt>Section</dt>
                    <dd>{citation.section_heading}</dd>
                  </div>
                )}
              </dl>
              <figure className="passage">
                <figcaption className="passage__caption">The passage this claim cites</figcaption>
                <p className="passage__text" data-testid="citation-passage">{citation.passage_text}</p>
              </figure>
            </>
          ) : (
            <p className="notice notice--error">The source for this claim could not be resolved.</p>
          )}

          <nav className="drawer__nav" aria-label="Other claims">
            <button type="button" className="button" disabled={claim.number <= 1} onClick={() => onStep(-1)}>Previous claim</button>
            <span className="drawer__count">{claim.number} of {total}</span>
            <button type="button" className="button" disabled={claim.number >= total} onClick={() => onStep(1)}>Next claim</button>
          </nav>
        </div>
      )}
    </dialog>
  );
}
