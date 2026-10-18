import { spawn } from "node:child_process";
import { createServer, type Server } from "node:http";
import type { AddressInfo } from "node:net";
import path from "node:path";
import { expect, test } from "@playwright/test";

// T20 B5 (T15-1): the PDF renderer itself, run as the API runs it (`node render-pdf.mjs`, the
// request on stdin), against a local page that stands in for the Report viewer. The page
// starts with its print Sources `loading` and settles later, well after the network is idle,
// so a PDF printed before the settled state would have the wrong page count. Offline: the
// page is served from this process and needs no app or backend.

const SCRIPT = path.join(__dirname, "..", "scripts", "render-pdf.mjs");
const SETTLE_MS = 2_000; // after `networkidle`, which needs 500 ms without requests
const EXISTING_TIMEOUT_MS = 30_000; // render-pdf.mjs: page.setDefaultTimeout(30_000)
const SETTLED_PAGES = 3; // a settled page adds two forced page breaks; `loading` is one page

function page(settleTo: "ready" | "failed" | null): string {
  const settle = settleTo === null ? "" : `
    setTimeout(() => {
      const sources = document.querySelector(".report__print-sources");
      sources.insertAdjacentHTML("beforeend",
        '<p style="break-before: page">${settleTo}</p><p style="break-before: page">end</p>');
      sources.setAttribute("data-print-sources-state", "${settleTo}");
    }, ${SETTLE_MS});`;
  return `<!doctype html><html><body>
    <article class="report"><h1>Report</h1>
      <section class="report__print-sources" data-print-sources-state="loading"><h2>Sources</h2>
        <p>Loading Sources…</p></section>
    </article>
    <script>${settle}</script>
  </body></html>`;
}

let server: Server;
let origin: string;

test.beforeAll(async () => {
  server = createServer((req, res) => {
    const settleTo = req.url === "/ready" ? "ready" : req.url === "/failed" ? "failed" : null;
    res.writeHead(200, { "content-type": "text/html; charset=utf-8" });
    res.end(page(settleTo));
  });
  await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
  origin = `http://127.0.0.1:${(server.address() as AddressInfo).port}`;
});

test.afterAll(async () => {
  await new Promise((resolve) => server.close(resolve));
});

interface Rendered { code: number | null; pdf: Buffer; stderr: string; elapsedMs: number }

function render(url: string): Promise<Rendered> {
  const started = Date.now();
  const child = spawn(process.execPath, [SCRIPT], { stdio: ["pipe", "pipe", "pipe"] });
  const out: Buffer[] = [];
  let stderr = "";
  child.stdout.on("data", (chunk: Buffer) => out.push(chunk));
  child.stderr.on("data", (chunk: Buffer) => { stderr += chunk.toString(); });
  child.stdin.end(JSON.stringify({ url, cookieName: "insightforge_persona", cookieValue: "test" }));
  return new Promise((resolve) => child.on("close", (code) =>
    resolve({ code, pdf: Buffer.concat(out), stderr, elapsedMs: Date.now() - started })));
}

function pageCount(pdf: Buffer): number {
  return (pdf.toString("latin1").match(/\/Type\s*\/Page(?![a-zA-Z])/g) ?? []).length;
}

for (const state of ["ready", "failed"] as const) {
  test(`prints only once the print Sources are ${state}`, async () => {
    const rendered = await render(`${origin}/${state}`);
    expect(rendered.code, rendered.stderr).toBe(0);
    expect(rendered.pdf.subarray(0, 5).toString("latin1")).toBe("%PDF-");
    expect(pageCount(rendered.pdf)).toBe(SETTLED_PAGES);
    expect(rendered.elapsedMs).toBeGreaterThanOrEqual(SETTLE_MS);
  });
}

test("fails within its existing timeout, with no PDF, while the Sources stay loading", async () => {
  test.setTimeout(EXISTING_TIMEOUT_MS + 45_000);
  const rendered = await render(`${origin}/loading`);
  expect(rendered.code).not.toBe(0);
  expect(rendered.pdf.length).toBe(0);
  expect(rendered.stderr).toMatch(/Timeout 30000ms exceeded/);
  expect(rendered.stderr).toMatch(/data-print-sources-state/);
  expect(rendered.elapsedMs).toBeGreaterThanOrEqual(EXISTING_TIMEOUT_MS);
  expect(rendered.elapsedMs).toBeLessThan(EXISTING_TIMEOUT_MS + 30_000);
});
