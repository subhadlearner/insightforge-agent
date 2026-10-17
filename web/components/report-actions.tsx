"use client";

import { useState } from "react";
import { createShare, downloadReport, revokeShare, type ShareCreated } from "@/lib/api";

type Busy = "md" | "pdf" | "share" | "revoke" | null;

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
  const [share, setShare] = useState<ShareCreated | null>(null);
  const [message, setMessage] = useState<string | null>(null);

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
      const next = await createShare(reportId);
      setShare(next);
      setMessage("Public share link created.");
    } catch (e) {
      setMessage((e as Error).message);
    } finally {
      setBusy(null);
    }
  }

  async function revoke() {
    if (!share) return;
    setBusy("revoke");
    setMessage(null);
    try {
      await revokeShare(reportId, share.share_id);
      setShare(null);
      setMessage("Share link revoked. It can no longer be opened.");
    } catch (e) {
      setMessage((e as Error).message);
    } finally {
      setBusy(null);
    }
  }

  const publicUrl = share && typeof window !== "undefined"
    ? `${window.location.origin}${share.path}`
    : null;

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
        {!share ? (
          <button className="button" type="button" disabled={busy !== null} onClick={create}>
            {busy === "share" ? "Creating link…" : "Create share link"}
          </button>
        ) : (
          <button className="button" type="button" disabled={busy !== null} onClick={revoke}>
            {busy === "revoke" ? "Revoking…" : "Revoke share link"}
          </button>
        )}
      </div>
      {publicUrl && (
        <p className="report-actions__share">
          Public link: <a href={share!.path}>{publicUrl}</a>
          {" "}· expires {new Date(share!.expires_at).toLocaleDateString()}
        </p>
      )}
      {message && <p className="report-actions__status" aria-live="polite">{message}</p>}
    </section>
  );
}
