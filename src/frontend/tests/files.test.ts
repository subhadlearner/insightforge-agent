import { describe, expect, it } from "vitest";
import { addPdfs, encodeDocuments, formatBytes, MAX_DOCUMENT_BYTES, MAX_DOCUMENTS } from "@/lib/files";

function pdf(name: string, size = 10, type = "application/pdf"): File {
  const file = new File(["%PDF-1.7"], name, { type });
  Object.defineProperty(file, "size", { value: size });
  return file;
}

describe("addPdfs", () => {
  it("accepts PDFs and keeps the File objects themselves", () => {
    const a = pdf("a.pdf");
    const { files, rejected } = addPdfs([], [a]);
    expect(files).toEqual([a]);
    expect(rejected).toEqual([]);
  });

  it("rejects files that are not PDFs", () => {
    const { files, rejected } = addPdfs([], [pdf("notes.txt", 10, "text/plain")]);
    expect(files).toEqual([]);
    expect(rejected).toEqual([{ name: "notes.txt", reason: "not a PDF" }]);
  });

  it("accepts a .pdf whose browser type is empty", () => {
    expect(addPdfs([], [pdf("scan.PDF", 10, "")]).files).toHaveLength(1);
  });

  it("rejects a PDF over 20 MB", () => {
    const { rejected } = addPdfs([], [pdf("big.pdf", MAX_DOCUMENT_BYTES + 1)]);
    expect(rejected).toEqual([{ name: "big.pdf", reason: "larger than 20 MB" }]);
    expect(addPdfs([], [pdf("edge.pdf", MAX_DOCUMENT_BYTES)]).files).toHaveLength(1);
  });

  it("stops at 10 documents", () => {
    const existing = Array.from({ length: MAX_DOCUMENTS - 1 }, (_, i) => pdf(`${i}.pdf`));
    const { files, rejected } = addPdfs(existing, [pdf("ten.pdf"), pdf("eleven.pdf")]);
    expect(files).toHaveLength(MAX_DOCUMENTS);
    expect(rejected).toEqual([{ name: "eleven.pdf", reason: "the limit is 10 documents" }]);
  });

  it("does not add the same file twice", () => {
    const a = pdf("a.pdf");
    expect(addPdfs([a], [pdf("a.pdf")]).files).toHaveLength(1);
  });
});

describe("encodeDocuments", () => {
  it("encodes each file once as base64 with its filename", async () => {
    const file = new File(["%PDF-hello"], "a.pdf", { type: "application/pdf" });
    expect(await encodeDocuments([file])).toEqual([
      { filename: "a.pdf", content_base64: btoa("%PDF-hello") }]);
  });
});

describe("formatBytes", () => {
  it("formats sizes for people", () => {
    expect(formatBytes(900)).toBe("900 B");
    expect(formatBytes(1536)).toBe("1.5 KB");
    expect(formatBytes(5 * 1024 * 1024)).toBe("5.0 MB");
  });
});
