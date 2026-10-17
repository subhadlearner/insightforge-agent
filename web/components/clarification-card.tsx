"use client";

// The Run's one clarifying question. GET /api/research/{id}/clarification is the authority
// for the question, its deadline and its resolution; the progress stream only says when to
// look again. The countdown is presentation: reaching zero only re-reads the clarification,
// because the server's durable timeout decides whether the question timed out. A 409 on the
// answer means it was already resolved (another answer, or the timeout); the card re-reads
// the clarification and shows what happened, and never sends the answer again.

import { useCallback, useEffect, useState, type FormEvent } from "react";
import { answerClarification, ApiError, getClarification, type ClarificationOut } from "@/lib/api";
import { formatCountdown } from "@/lib/format";
import type { ClarificationProgress } from "@/lib/run-reducer";

type View =
  | { kind: "loading" }
  | { kind: "error"; message: string }
  | { kind: "open"; clarification: ClarificationOut };

type Outcome = "sent" | "late" | null;

export function ClarificationCard({ runId, progress, ended = false }: {
  runId: string;
  progress: ClarificationProgress;
  ended?: boolean; // the Run is over: only say how the question was resolved
}) {
  const [view, setView] = useState<View>({ kind: "loading" });
  const [answer, setAnswer] = useState("");
  const [sending, setSending] = useState(false);
  const [outcome, setOutcome] = useState<Outcome>(null);
  const [answerError, setAnswerError] = useState<string | null>(null);
  const [now, setNow] = useState(() => Date.now());

  const load = useCallback(async () => {
    try {
      setView({ kind: "open", clarification: await getClarification(runId) });
    } catch (e) {
      setView({ kind: "error", message: (e as Error).message });
    }
  }, [runId]);

  // Read it on mount and whenever the stream reports a resolution.
  useEffect(() => {
    void load();
  }, [load, progress.resolution, progress.status]);

  const clarification = view.kind === "open" ? view.clarification : null;
  const resolution = clarification?.resolution ?? null;
  const countdown = clarification ? formatCountdown(clarification.deadline, now) : null;
  const deadlinePassed = clarification !== null && resolution === null && countdown === null;

  useEffect(() => {
    if (!clarification || resolution) return;
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(timer);
  }, [clarification, resolution]);

  // At the visual deadline, ask the server what happened instead of assuming.
  useEffect(() => {
    if (deadlinePassed) void load();
  }, [deadlinePassed, load]);

  async function submit(e: FormEvent) {
    e.preventDefault();
    if (sending || !answer.trim()) return;
    setSending(true);
    setAnswerError(null);
    try {
      await answerClarification(runId, answer.trim());
      setOutcome("sent");
      await load();
    } catch (err) {
      if (err instanceof ApiError && err.status === 409) {
        setOutcome("late");
        await load(); // reconcile with the resolution that won
      } else {
        setAnswerError((err as Error).message);
      }
    } finally {
      setSending(false);
    }
  }

  if (view.kind === "loading") {
    return <section className="question" aria-live="polite"><p className="question__status">Loading the question…</p></section>;
  }
  if (view.kind === "error") {
    return (
      <section className="question" role="alert">
        <p className="question__status">The question could not be loaded: {view.message}</p>
        <button type="button" className="button" onClick={() => void load()}>Try again</button>
      </section>
    );
  }

  const c = view.clarification;
  return (
    <section className="question" aria-labelledby="question-title">
      <h2 id="question-title" className="question__title">The research needs one detail from you</h2>
      <blockquote className="question__text">{c.question}</blockquote>

      {resolution === null && !ended && (
        <form onSubmit={submit} className="question__form">
          <label htmlFor="answer" className="field__label">Your answer</label>
          <textarea
            id="answer"
            className="question__input"
            rows={3}
            value={answer}
            maxLength={2000}
            disabled={sending}
            onChange={(e) => setAnswer(e.target.value)}
          />
          <div className="question__actions">
            <button type="submit" className="button button--primary" disabled={sending || !answer.trim()}>
              {sending ? "Sending…" : "Send answer"}
            </button>
            <p className="question__deadline" aria-live="off">
              {countdown ? <>Answer within <strong>{countdown}</strong>, or research continues without it.</>
                : "The deadline has passed. The server decides whether research continues without your answer."}
            </p>
          </div>
          {outcome === "late" && (
            <p className="notice notice--quiet" role="status">
              Your answer arrived after the question closed. Research continues without it.
            </p>
          )}
          {answerError && <p className="notice notice--error" role="alert">{answerError}</p>}
        </form>
      )}

      {resolution === "answered" && (
        <p className="question__resolved" role="status">
          {ended ? "Your answer was used for this research."
            : outcome === "late" ? "Another answer was accepted first. Research is continuing with it."
            : "Answer received. Resuming research…"}
        </p>
      )}
      {resolution === "timed_out" && (
        <p className="question__resolved" role="status">
          {outcome === "late" ? "Your answer arrived after the question timed out. " : "The question timed out. "}
          {ended ? "Research went ahead on a best-effort reading of the Brief."
            : "Research continues on a best-effort reading of the Brief."}
        </p>
      )}
    </section>
  );
}
