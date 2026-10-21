"use client";

import { usePersona } from "./persona-context";

/** The header control: who the research is done as. A demo identity, not a sign-in. */
export function PersonaPicker() {
  const { status, error, personas, current, select, reload } = usePersona();

  if (status === "loading") {
    return <p className="persona persona--quiet" aria-live="polite">Loading personas…</p>;
  }
  if (status === "error") {
    return (
      <p className="persona persona--error" role="alert">
        Personas could not be loaded: {error}{" "}
        <button type="button" className="link-button" onClick={reload}>Try again</button>
      </p>
    );
  }
  return (
    <div className="persona">
      <label htmlFor="persona-select" className="persona__label">Researching as</label>
      <select
        id="persona-select"
        className="persona__select"
        value={current?.id ?? ""}
        onChange={(e) => void select(e.target.value)}
      >
        {!current && <option value="" disabled>Choose a persona</option>}
        {personas.map((p) => (
          <option key={p.id} value={p.id}>{p.display_name}</option>
        ))}
      </select>
      {error && <span className="persona__error" role="alert">{error}</span>}
    </div>
  );
}
