"use client";

import { SOURCE_TYPES, type SourceToggles, type SourceType } from "@/lib/api";

const LABELS: Record<SourceType, { name: string; hint: string }> = {
  web: { name: "Web", hint: "Search and read pages from the web." },
  documents: { name: "Your documents", hint: "Read the PDFs you attach below." },
  memory: { name: "Earlier Reports", hint: "Use your previous Reports as background." },
};

export function SourceTogglesField({ value, onChange }: {
  value: SourceToggles;
  onChange: (next: SourceToggles) => void;
}) {
  return (
    <fieldset className="field sources">
      <legend className="field__label">Sources</legend>
      <div className="sources__list">
        {SOURCE_TYPES.map((type) => (
          <label key={type} className="source">
            <input
              type="checkbox"
              checked={Boolean(value[type])}
              onChange={(e) => onChange({ ...value, [type]: e.target.checked })}
            />
            <span className="source__text">
              <span className="source__name">{LABELS[type].name}</span>
              <span className="source__hint">{LABELS[type].hint}</span>
            </span>
          </label>
        ))}
      </div>
    </fieldset>
  );
}

/** A persona's defaults as a complete toggle set: omitted types are off, and a persona with
 *  no defaults means web only (the backend's rule for omitted toggles). */
export function togglesFor(defaults: Record<string, boolean> | undefined): SourceToggles {
  const has = defaults && Object.keys(defaults).length > 0;
  return Object.fromEntries(
    SOURCE_TYPES.map((t) => [t, has ? Boolean(defaults[t]) : t === "web"]),
  ) as SourceToggles;
}
