"use client";

// The Report's table of contents (T20 B6), shared by the owner's Report and the public share.
// A native list of same-page links: Tab reaches each one and Enter follows it. Following a link
// scrolls its heading into view (headings keep a scroll margin) and moves focus to it, so
// keyboard and screen-reader users land where they asked. It sits outside the printed article,
// and print CSS hides it.

import type { MouseEvent } from "react";
import type { TocEntry } from "@/lib/toc";

function follow(event: MouseEvent<HTMLAnchorElement>, id: string) {
  const target = document.getElementById(id);
  if (!target) return; // the browser's own jump, which finds nothing either
  event.preventDefault();
  const reduced = window.matchMedia?.("(prefers-reduced-motion: reduce)").matches ?? false;
  target.scrollIntoView?.({ behavior: reduced ? "auto" : "smooth", block: "start" });
  target.focus({ preventScroll: true });
  window.history.replaceState(null, "", `#${id}`);
}

export function ReportToc({ entries }: { entries: TocEntry[] }) {
  if (entries.length === 0) return null;
  return (
    <nav className="report-toc" aria-label="Report contents">
      <p className="report-toc__title" aria-hidden="true">Contents</p>
      <ol className="report-toc__list">
        {entries.map((e) => (
          <li key={e.id}>
            <a href={`#${e.id}`} onClick={(event) => follow(event, e.id)}>{e.label}</a>
          </li>
        ))}
      </ol>
    </nav>
  );
}
