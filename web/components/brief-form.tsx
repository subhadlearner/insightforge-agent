"use client";

import { useRouter } from "next/navigation";
import { useEffect, useState, type FormEvent } from "react";
import { ApiError, submitRun, type SourceToggles } from "@/lib/api";
import { BRIEF_MAX_CHARS, briefLength, fitBrief } from "@/lib/brief";
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
  // Set when the last edit was cut to the limit, so the status can say text was not added.
  const [truncated, setTruncated] = useState(false);
  const [toggles, setToggles] = useState<SourceToggles>(() => togglesFor(current?.source_toggles));
  const [files, setFiles] = useState<File[]>([]);
  const [submission, setSubmission] = useState<Submission>({ kind: "idle" });
  const submitting = submission.kind === "submitting";
  // Attached PDFs are only read when Your documents is on, so while any are attached it is
  // held on. The checkbox, the estimate and the submission all use this one value; removing
  // the last PDF returns to the User's own choice.
  const effective: SourceToggles = files.length ? { ...toggles, documents: true } : toggles;
  const used = briefLength(brief);
  const remaining = BRIEF_MAX_CHARS - used;
  const fits = used <= BRIEF_MAX_CHARS;
  // Announced only when it changes: on reaching the last 50, at the limit, and when an edit
  // was cut; never on every keystroke.
  const limitStatus = truncated
    ? `The Brief is limited to ${BRIEF_MAX_CHARS} characters. Text beyond the limit was not added.`
    : remaining === 0
      ? `Character limit reached: ${BRIEF_MAX_CHARS} of ${BRIEF_MAX_CHARS}.`
      : remaining <= 50
        ? "50 or fewer characters left."
        : "";

  // A different persona brings its own default sources; the same persona keeps the User's
  // edits (keyed by ID, not by the identity of the persona object).
  const defaults = current?.source_toggles;
  useEffect(() => {
    setToggles(togglesFor(defaults));
  }, [current?.id]);

  async function submit(e: FormEvent) {
    e.preventDefault();
    // The limit is on the raw text, as typed; the trimmed text is what is sent.
    if (submitting || !current || !brief.trim() || !fits) return;
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
          aria-describedby="brief-hint brief-count"
          rows={7}
          value={brief}
          disabled={submitting}
          placeholder="How did BYD's battery EV pricing compare with Tesla's in Europe during 2025?"
          onChange={(e) => {
            const fitted = fitBrief(brief, e.target.value);
            if (fitted.truncated) {
              // Set before React re-renders, so the caret stays where the kept text ends.
              e.target.value = fitted.value;
              e.target.setSelectionRange(fitted.caret, fitted.caret);
            }
            setTruncated(fitted.truncated);
            setBrief(fitted.value);
          }}
        />
        <p id="brief-count" className={`brief__count${remaining === 0 ? " brief__count--full" : ""}`}>
          <span aria-hidden="true" data-testid="brief-count">{used}/{BRIEF_MAX_CHARS}</span>
          <span className="visually-hidden">{`${used} of ${BRIEF_MAX_CHARS} characters used`}</span>
        </p>
        <p className="visually-hidden" role="status" data-testid="brief-limit-status">{limitStatus}</p>
        <SourceTogglesField
          value={effective}
          onToggle={(type, on) => setToggles((t) => ({ ...t, [type]: on }))}
          locked={files.length ? { documents: "On while PDFs are attached. Remove them to turn this off." } : {}}
        />
        <PdfPicker files={files} onChange={setFiles} disabled={submitting} />
      </div>

      <aside className="desk__side" aria-label="Before you submit">
        <RuntimeEstimate toggles={effective} documentCount={files.length} enabled={Boolean(current)} />
        <button type="submit" className="button button--primary" disabled={submitting || !current || !brief.trim() || !fits}>
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
