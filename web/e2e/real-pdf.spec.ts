import { readFileSync } from "node:fs";
import path from "node:path";
import { expect, test } from "@playwright/test";
import { API_ORIGIN, choosePersona, control } from "./support";

// T20 B6: one real PDF through the whole product, offline. The browser attaches the committed
// fictional annual report (samples/documents, 3 pages), Next rewrites the upload to FastAPI, the
// real Docling converts and chunks it, the real fastembed model indexes it into Qdrant, the Doc
// Reader's search retrieves it, and the Report cites it; the citation drawer must show the exact
// passage with the page and section Docling found. Only the chat models are scripted
// (tests/web_e2e/real_documents.py). The backend blocks every outbound connection, and this test
// asserts none was attempted. Without the cached model files the test is skipped, with the
// reason, rather than run against a stand-in.

const PDF = path.join(__dirname, "..", "..", "samples", "documents", "northwind-health-annual-report-2025.pdf");
const BRIEF = "Assess Northwind Health from its annual report: revenue, R&D spending and principal risks.";
// Page 2 of the PDF, under "Research and development" (samples/documents/make_sample_documents.py).
const FACT = "Research and development spending was 71 million dollars in fiscal 2025.";

interface Capabilities { real_documents: boolean; reason: string | null; blocked_network: string[] }

test("a real PDF is uploaded, parsed, retrieved, cited, and its passage shown with page and section", async ({ page, request }) => {
  test.setTimeout(300_000);
  const caps: Capabilities = await (await request.get(`${API_ORIGIN}/__test__/capabilities`)).json();
  test.skip(!caps.real_documents, `real document research is not wired: ${caps.reason}`);
  await control(request, "gate/open");

  await choosePersona(page, "alice");
  await page.getByLabel("Brief", { exact: true }).fill(BRIEF);
  await page.getByLabel("Documents", { exact: true }).setInputFiles(PDF);
  await expect(page.getByText(path.basename(PDF))).toBeVisible();
  await page.getByRole("checkbox", { name: /^Web/ }).setChecked(false);
  await page.getByRole("checkbox", { name: /^Earlier Reports/ }).setChecked(false);
  await expect(page.getByRole("checkbox", { name: /^Your documents/ })).toBeChecked();

  // The upload goes through the Next rewrite, as the real file's bytes.
  const submitted = page.waitForRequest((r) => r.url().endsWith("/api/research/run") && r.method() === "POST");
  await page.getByRole("button", { name: "Start research" }).click();
  const body = (await submitted).postDataJSON();
  expect(new URL((await submitted).url()).origin).toBe(new URL(page.url()).origin);
  expect(body.source_toggles).toMatchObject({ web: false, documents: true, memory: false });
  expect(body.documents).toHaveLength(1);
  expect(body.documents[0].filename).toBe(path.basename(PDF));
  expect(Buffer.from(body.documents[0].content_base64, "base64").equals(readFileSync(PDF))).toBe(true);
  await page.waitForURL(/\/runs\/run_/);
  const runId = decodeURIComponent(new URL(page.url()).pathname.split("/").pop()!);

  // Live progress from persisted events: the document is read, the plan is accepted, each
  // document Sub-task has a validated outcome.
  await expect(page.getByTestId("run-complete")).toBeVisible({ timeout: 240_000 });
  const rail = page.getByRole("list", { name: "Run progress" });
  await expect(rail.getByText("Reading documents", { exact: true })).toBeVisible();
  const log = page.getByRole("log", { name: "Run activity" });
  await expect(log).toContainText("Read an uploaded document");
  await expect(log).toContainText("Plan accepted: 3 Sub-tasks");
  await expect(log).toContainText("Searched the uploaded documents");
  for (const id of ["s1", "s2", "s3"]) {
    await expect(page.getByTestId(`subtask-${id}`)).toContainText("Done");
  }
  await expect(page.getByTestId("subtask-count")).toHaveText("Sub-tasks: 3 of 3 finished (3 done, 0 failed)");
  await expect(page.getByTestId("elapsed")).toContainText(/^Took /);

  // The Report states the page-2 fact, and its citation is that passage, page and section.
  await page.getByRole("link", { name: "Read the Report" }).click();
  await page.waitForURL(/\/reports\/rep_/);
  const reportId = decodeURIComponent(new URL(page.url()).pathname.split("/").pop()!);
  const claim = page.locator(".claim").filter({ hasText: FACT });
  await expect(claim).toHaveCount(1);
  const marker = claim.getByRole("button", { name: /^View source for claim \d+$/ });
  const number = (await marker.textContent())!.trim();
  await marker.click();
  const drawer = page.getByRole("dialog", { name: `Source for claim ${number}` });
  await expect(drawer).toBeVisible();
  await expect(drawer.getByTestId("citation-source")).toHaveText(path.basename(PDF));
  await expect(drawer.getByTestId("citation-passage")).toHaveText(FACT);
  await expect(drawer.getByTestId("citation-page")).toHaveText("2");
  await expect(drawer.getByTestId("citation-section")).toHaveText("Research and development");

  // The drawer shows exactly what the API resolved from the stored provenance.
  const citations = await (await page.request.get(`/api/reports/${reportId}/citations`)).json();
  const cited = citations[Number(number) - 1];
  expect(cited).toMatchObject({ source_title: path.basename(PDF), passage_text: FACT, page: 2, section_heading: "Research and development" });
  expect(citations.every((c: { source_title: string; page: number | null }) => c.source_title === path.basename(PDF) && c.page != null)).toBe(true);

  // The Run used the real stack and stayed offline.
  const events = await (await page.request.get(`/api/research/status?run_id=${encodeURIComponent(runId)}`)).text();
  const ingested = events.split("\n\n").map((f) => f.match(/^data: (.*)$/m)?.[1]).filter(Boolean)
    .map((d) => JSON.parse(d!)).find((e) => e.type === "document_ingested");
  expect(ingested.payload).toMatchObject({ filename: path.basename(PDF), pages: 3 });
  expect(ingested.payload.chunks).toBeGreaterThan(3);
  const after: Capabilities = await (await request.get(`${API_ORIGIN}/__test__/capabilities`)).json();
  expect(after.blocked_network).toEqual([]);
});
