"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { listReports, type ReportListItem } from "@/lib/api";
import { formatDate } from "@/lib/format";
import { usePersona } from "./persona-context";

/** The selected persona's Reports, newest first, so earlier work is one click away. */
export function RecentReports() {
  const { current } = usePersona();
  const [reports, setReports] = useState<ReportListItem[] | null>(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    if (!current) {
      setReports(null);
      return;
    }
    let live = true;
    setReports(null);
    setFailed(false);
    listReports()
      .then((list) => live && setReports(list.slice(0, 8)))
      .catch(() => live && setFailed(true));
    return () => {
      live = false;
    };
  }, [current?.id]); // re-read when the persona changes

  if (!current) return null;
  return (
    <section className="recent" aria-labelledby="recent-title">
      <h2 id="recent-title" className="recent__title">Your Reports</h2>
      {failed && <p className="recent__empty">Your Reports could not be loaded.</p>}
      {!failed && reports === null && <p className="recent__empty">Loading…</p>}
      {reports?.length === 0 && <p className="recent__empty">No Reports yet. Your first one will appear here.</p>}
      {reports && reports.length > 0 && (
        <ul className="recent__list">
          {reports.map((r) => (
            <li key={r.id}>
              <Link href={`/reports/${encodeURIComponent(r.id)}`} className="recent__link">{r.title || r.brief}</Link>
              <span className="recent__date">{formatDate(r.created_at)}</span>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
