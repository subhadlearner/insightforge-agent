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
  return text.normalize("NFKD").replace(/[̀-ͯ]/g, "").toLowerCase()
    .replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "");
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
