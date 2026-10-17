"use client";

import { useRouter } from "next/navigation";
import { useEffect, useState, type FormEvent } from "react";
import { ApiError, submitRun, type SourceToggles } from "@/lib/api";
import { encodeDocuments } from "@/lib/files";
import { PdfPicker } from "./pdf-picker";
import { usePersona } from "./persona-context";
import { RuntimeEstimate } from "./runtime-estimate";
import { SourceTogglesField, togglesFor } from "./source-toggles";

type Submission =
  | { kind: "idle" }
  | { kind: "submitting" }
  | { kind: "rejected"; title: string; message: string };

function describe(e: unknown): { title: string; message: string } {
  if (!(e instanceof ApiError)) return { title: "The Run was not started", message: String(e) };
  switch (e.status) {
    case 422:
      return { title: "This Brief can't be researched as submitted", message: e.message };
    case 503:
      return {
        title: "Research is busy right now",
        message: `${e.message}${e.retryAfterSeconds ? ` Try again in about ${e.retryAfterSeconds} seconds.` : ""}`,
      };
    case 401:
      return { title: "Choose a persona first", message: "Pick who you are researching as, then submit again." };
    case 0:
      return {
        title: "No answer from the server",
        message: "Your Brief is still here. The Run may or may not have started, so check your recent Reports before submitting again.",
      };
    default:
      return { title: "The Run was not started", message: e.message };
  }
}

export function BriefForm() {
  const router = useRouter();
  const { current } = usePersona();
  const [brief, setBrief] = useState("");
  const [toggles, setToggles] = useState<SourceToggles>(() => togglesFor(current?.source_toggles));
  const [files, setFiles] = useState<File[]>([]);
  const [submission, setSubmission] = useState<Submission>({ kind: "idle" });
  const submitting = submission.kind === "submitting";
  // Attached PDFs are only read when Your documents is on, so while any are attached it is
  // held on. The checkbox, the estimate and the submission all use this one value; removing
  // the last PDF returns to the User's own choice.
  const effective: SourceToggles = files.length ? { ...toggles, documents: true } : toggles;

  // A different persona brings its own default sources; the same persona keeps the User's
  // edits (keyed by ID, not by the identity of the persona object).
  const defaults = current?.source_toggles;
  useEffect(() => {
    setToggles(togglesFor(defaults));
  }, [current?.id]);

  async function submit(e: FormEvent) {
    e.preventDefault();
    if (submitting || !current || !brief.trim()) return;
    setSubmission({ kind: "submitting" });
    try {
      // Encoded once, here; never retried automatically (a retry could start a second Run).
      const documents = await encodeDocuments(files);
      const accepted = await submitRun(brief.trim(), effective, documents);
      router.push(`/runs/${encodeURIComponent(accepted.run_id)}`);
    } catch (error) {
      setSubmission({ kind: "rejected", ...describe(error) });
    }
  }

  return (
    <form className="desk" onSubmit={submit} aria-busy={submitting}>
      <div className="desk__main">
        <label htmlFor="brief" className="brief__label">Brief</label>
        <p id="brief-hint" className="brief__hint">
          Ask one research question. Name the market, the companies or products, and the period you care about.
        </p>
        <textarea
          id="brief"
          className="brief__input"
          aria-describedby="brief-hint"
          rows={7}
          value={brief}
          disabled={submitting}
          placeholder="How did BYD's battery EV pricing compare with Tesla's in Europe during 2025?"
          onChange={(e) => setBrief(e.target.value)}
        />
        <SourceTogglesField
          value={effective}
          onChange={setToggles}
          locked={files.length ? { documents: "On while PDFs are attached. Remove them to turn this off." } : {}}
        />
        <PdfPicker files={files} onChange={setFiles} disabled={submitting} />
      </div>

      <aside className="desk__side" aria-label="Before you submit">
        <RuntimeEstimate toggles={effective} documentCount={files.length} enabled={Boolean(current)} />
        <button type="submit" className="button button--primary" disabled={submitting || !current || !brief.trim()}>
          {submitting ? "Starting research…" : "Start research"}
        </button>
        {!current && <p className="desk__note">Choose a persona at the top to start.</p>}
        {submission.kind === "rejected" && (
          <div className="notice notice--error" role="alert">
            <p className="notice__title">{submission.title}</p>
            <p>{submission.message}</p>
          </div>
        )}
      </aside>
    </form>
  );
}
