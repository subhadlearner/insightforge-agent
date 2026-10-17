"use client";

import { useEffect, useState } from "react";
import { createShare, downloadReport, listShares, revokeShare, type ShareCreated } from "@/lib/api";

type Busy = "md" | "pdf" | "share" | string | null;

function save(blob: Blob, filename: string) {
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}

export function ReportActions({ reportId }: { reportId: string }) {
  const [busy, setBusy] = useState<Busy>(null);
  const [shares, setShares] = useState<ShareCreated[]>([]);
  const [loadingShares, setLoadingShares] = useState(true);
  const [message, setMessage] = useState<string | null>(null);

  useEffect(() => {
    let active = true;
    setLoadingShares(true);
    setShares([]);
    listShares(reportId).then((found) => {
      if (active) setShares(found);
    }).catch((e: Error) => {
      if (active) setMessage(e.message);
    }).finally(() => {
      if (active) setLoadingShares(false);
    });
    return () => { active = false; };
  }, [reportId]);

  async function download(format: "md" | "pdf") {
    setBusy(format);
    setMessage(null);
    try {
      save(await downloadReport(reportId, format), `${reportId}.${format}`);
      setMessage(format === "md" ? "Markdown downloaded." : "PDF downloaded.");
    } catch (e) {
      setMessage((e as Error).message);
    } finally {
      setBusy(null);
    }
  }

  async function create() {
    setBusy("share");
    setMessage(null);
    try {
      const created = await createShare(reportId);
      setShares((prev) => [...prev, created]);
      setMessage("Public share link created.");
    } catch (e) {
      setMessage((e as Error).message);
    } finally {
      setBusy(null);
    }
  }

  async function revoke(shareId: string) {
    setBusy(shareId);
    setMessage(null);
    try {
      await revokeShare(reportId, shareId);
      setShares((prev) => prev.filter((share) => share.share_id !== shareId));
      setMessage("Share link revoked. It can no longer be opened.");
    } catch (e) {
      setMessage((e as Error).message);
    } finally {
      setBusy(null);
    }
  }

  return (
    <section className="report-actions" aria-label="Report actions">
      <div className="report-actions__buttons">
        <button className="button" type="button" disabled={busy !== null}
          onClick={() => download("md")}>
          {busy === "md" ? "Exporting Markdown…" : "Export Markdown"}
        </button>
        <button className="button" type="button" disabled={busy !== null}
          onClick={() => download("pdf")}>
          {busy === "pdf" ? "Rendering PDF…" : "Export PDF"}
        </button>
        <button className="button" type="button" disabled={busy !== null || loadingShares} onClick={create}>
          {busy === "share" ? "Creating link…" : "Create share link"}
        </button>
      </div>
      {loadingShares && <p className="report-actions__status">Loading active share links…</p>}
      {shares.length > 0 && (
        <ul aria-label="Active share links">
          {shares.map((share) => (
            <li key={share.share_id} className="report-actions__share">
              Public link: <a href={share.path}>{typeof window !== "undefined" ? window.location.origin : ""}{share.path}</a>
              {" "}· expires {new Date(share.expires_at).toLocaleDateString()}{" "}
              <button className="link-button" type="button" disabled={busy !== null}
                onClick={() => revoke(share.share_id)}>
                {busy === share.share_id ? "Revoking…" : "Revoke share link"}
              </button>
            </li>
          ))}
        </ul>
      )}
      {message && <p className="report-actions__status" aria-live="polite">{message}</p>}
    </section>
  );
}
