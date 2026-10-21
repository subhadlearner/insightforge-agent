// Anchors for the Report's table of contents (T20 B6). Pure.
//
// Each rendered section gets `section-<slug of its heading>`, numbered -2, -3 … when a heading
// repeats (the Writer may give two sections the same heading; they are never merged here), so
// every anchor is unique and the same Report always gets the same anchors. The `section-`
// prefix keeps them apart from the fixed anchors below.

export const SUMMARY_ANCHOR = "executive-summary";
export const GAPS_ANCHOR = "gaps-and-limitations";
export const WEB_SOURCES_ANCHOR = "web-sources";

export interface TocEntry {
  id: string;
  label: string;
}

function slug(text: string): string {
  return text.normalize("NFKD").replace(/[\u0300-\u036f]/g, "").toLowerCase()
    .replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "");
}

/** The contents of a Report: Executive Summary when present, each rendered section, Gaps &
 *  limitations when present and, on a public share, Web Sources. `anchors` are the sections'. */
export function reportContents(report: { summary: string | null | undefined; headings: readonly string[]; gaps: number; webSources?: boolean }): { anchors: string[]; entries: TocEntry[] } {
  const anchors = sectionAnchors(report.headings);
  const entries: TocEntry[] = [
    ...(report.summary ? [{ id: SUMMARY_ANCHOR, label: "Executive Summary" }] : []),
    ...report.headings.map((label, i) => ({ id: anchors[i], label })),
    ...(report.gaps > 0 ? [{ id: GAPS_ANCHOR, label: "Gaps & limitations" }] : []),
    ...(report.webSources ? [{ id: WEB_SOURCES_ANCHOR, label: "Web Sources" }] : []),
  ];
  return { anchors, entries };
}

/** One anchor per heading, in order: readable, unique and stable. */
export function sectionAnchors(headings: readonly string[]): string[] {
  const used = new Set<string>();
  return headings.map((heading, i) => {
    const s = slug(heading);
    const base = s ? `section-${s}` : `section-${i + 1}`;
    let id = base;
    for (let n = 2; used.has(id); n++) id = `${base}-${n}`;
    used.add(id);
    return id;
  });
}
