"use client";

import { useId, useRef, useState } from "react";
import { addPdfs, formatBytes, MAX_DOCUMENT_MB, MAX_DOCUMENTS, type Rejected } from "@/lib/files";

export function PdfPicker({ files, onChange, disabled }: {
  files: File[];
  onChange: (files: File[]) => void;
  disabled?: boolean;
}) {
  const inputId = useId();
  const input = useRef<HTMLInputElement>(null);
  const [rejected, setRejected] = useState<Rejected[]>([]);

  function add(list: FileList | null) {
    if (!list) return;
    const result = addPdfs(files, Array.from(list));
    setRejected(result.rejected);
    onChange(result.files);
    if (input.current) input.current.value = ""; // the same file can be picked again later
  }

  return (
    <div className="field documents">
      <div className="documents__head">
        <label htmlFor={inputId} className="field__label">Documents</label>
        <span className="field__aside">{files.length} of {MAX_DOCUMENTS} PDFs, up to {MAX_DOCUMENT_MB} MB each</span>
      </div>
      {files.length > 0 && (
        <ul className="documents__list">
          {files.map((file, i) => (
            <li key={`${file.name}-${file.size}`} className="document">
              <span className="document__name">{file.name}</span>
              <span className="document__size">{formatBytes(file.size)}</span>
              <button
                type="button"
                className="link-button"
                disabled={disabled}
                aria-label={`Remove ${file.name}`}
                onClick={() => onChange(files.filter((_, j) => j !== i))}
              >
                Remove
              </button>
            </li>
          ))}
        </ul>
      )}
      <input
        ref={input}
        id={inputId}
        className="documents__input visually-hidden"
        type="file"
        accept="application/pdf,.pdf"
        multiple
        disabled={disabled || files.length >= MAX_DOCUMENTS}
        onChange={(e) => add(e.target.files)}
      />
      <label htmlFor={inputId} className="button documents__add" aria-hidden="true">
        {files.length ? "Add more PDFs" : "Attach PDFs"}
      </label>
      {rejected.length > 0 && (
        <ul className="documents__rejected" role="alert">
          {rejected.map((r) => (
            <li key={r.name}>{r.name} was not added: {r.reason}.</li>
          ))}
        </ul>
      )}
    </div>
  );
}
