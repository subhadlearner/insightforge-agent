// Uploaded PDFs, kept as browser File objects until submit. The limits mirror
// domain/documents.py for quick feedback; the backend still checks size, count and the PDF
// signature, and its answer is the one that counts.

import type { DocumentIn } from "./api";

export const MAX_DOCUMENTS = 10;
export const MAX_DOCUMENT_MB = 20;
export const MAX_DOCUMENT_BYTES = MAX_DOCUMENT_MB * 1024 * 1024;

export interface Rejected {
  name: string;
  reason: string;
}

function isPdf(file: File): boolean {
  return file.type === "application/pdf" || (file.type === "" && /\.pdf$/i.test(file.name));
}

function sameFile(a: File, b: File): boolean {
  return a.name === b.name && a.size === b.size;
}

/** Adds what fits; says why anything else was left out. */
export function addPdfs(existing: File[], incoming: File[]): { files: File[]; rejected: Rejected[] } {
  const files = [...existing];
  const rejected: Rejected[] = [];
  for (const file of incoming) {
    if (files.some((f) => sameFile(f, file))) continue;
    if (!isPdf(file)) rejected.push({ name: file.name, reason: "not a PDF" });
    else if (file.size > MAX_DOCUMENT_BYTES) rejected.push({ name: file.name, reason: `larger than ${MAX_DOCUMENT_MB} MB` });
    else if (files.length >= MAX_DOCUMENTS) rejected.push({ name: file.name, reason: `the limit is ${MAX_DOCUMENTS} documents` });
    else files.push(file);
  }
  return { files, rejected };
}

function toBase64(bytes: Uint8Array): string {
  let binary = "";
  const step = 0x8000;
  for (let i = 0; i < bytes.length; i += step) {
    binary += String.fromCharCode(...bytes.subarray(i, i + step));
  }
  return btoa(binary);
}

/** Reads and encodes the files. Called once, when the Run is submitted. */
export async function encodeDocuments(files: File[]): Promise<DocumentIn[]> {
  return Promise.all(files.map(async (file) => ({
    filename: file.name,
    content_base64: toBase64(new Uint8Array(await file.arrayBuffer())),
  })));
}

export function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}
