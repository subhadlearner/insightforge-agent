"use client";

// Loads one Report and its citations separately, so a citation failure (e.g. 500 for
// inconsistent provenance) leaves the Report readable.

import Link from "next/link";
import { useEffect, useState } from "react";
import { ApiError, getCitations, getReport } from "@/lib/api";
import { parseReport, type ParsedReport } from "@/lib/report";
import { ReportViewer, type CitationsState } from "./report-viewer";

type ReportState =
  | { kind: "loading" }
  | { kind: "missing" }
  | { kind: "error"; message: string }
  | { kind: "ready"; report: ParsedReport };

export function ReportView({ reportId }: { reportId: string }) {
  const [report, setReport] = useState<ReportState>({ kind: "loading" });
  const [citations, setCitations] = useState<CitationsState>({ kind: "loading" });

  useEffect(() => {
    let live = true;
    getReport(reportId)
      .then((out) => live && setReport({ kind: "ready", report: parseReport(out) }))
      .catch((e: Error) => {
        if (!live) return;
        setReport(e instanceof ApiError && e.status === 404 ? { kind: "missing" } : { kind: "error", message: e.message });
      });
    getCitations(reportId)
      .then((list) => live && setCitations({ kind: "ready", citations: list }))
      .catch((e: Error) => live && setCitations({
        kind: "failed", status: e instanceof ApiError ? e.status : 0, message: e.message,
      }));
    return () => {
      live = false;
    };
  }, [reportId]);

  if (report.kind === "loading") return <p className="page-status" aria-live="polite">Loading the Report…</p>;
  if (report.kind === "missing") {
    return (
      <div className="page-status">
        <h1 className="page-status__title">Report not found</h1>
        <p>There is no Report with this link for the persona you are researching as.</p>
        <Link href="/">Back to your Briefs</Link>
      </div>
    );
  }
  if (report.kind === "error") {
    return (
      <div className="page-status" role="alert">
        <h1 className="page-status__title">The Report could not be shown</h1>
        <p>{report.message}</p>
      </div>
    );
  }
  return <ReportViewer report={report.report} citations={citations} />;
}
