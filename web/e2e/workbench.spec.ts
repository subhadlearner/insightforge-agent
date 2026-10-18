import { expect, test } from "@playwright/test";
import {
  BRIEF, choosePersona, control, expectCurrentStep, QUESTION, rail, submitBrief, VAGUE,
} from "./support";

test.beforeEach(async ({ request }) => {
  await control(request, "gate/open");
  await control(request, "clock/reset");
});

test.afterEach(async ({ request }) => {
  await control(request, "gate/open"); // never leave a Run held for the next test
});

test("the persona cookie is HttpOnly and rides on every /api call through the rewrite", async ({ page, context, browser }) => {
  await choosePersona(page, "bob");

  const cookie = (await context.cookies()).find((c) => c.name === "insightforge_persona");
  expect(cookie, "the session cookie is set on the Next origin").toBeTruthy();
  expect(cookie!.httpOnly).toBe(true);
  expect(cookie!.domain).toBe("localhost");
  expect(await page.evaluate(() => document.cookie)).not.toContain("insightforge_persona");

  // Later same-origin calls carry it: FastAPI resolves the persona from it.
  const session = await page.request.get("/api/session");
  expect(session.status()).toBe(200);
  expect((await session.json()).id).toBe("bob");
  expect((await page.request.get("/api/reports")).status()).toBe(200);

  // A reload restores the selection from the cookie alone.
  await page.reload();
  await expect(page.getByLabel("Researching as")).toHaveValue("bob");

  // A browser without the cookie is not that persona.
  const stranger = await browser.newContext();
  expect((await stranger.request.get("http://localhost:3200/api/session")).status()).toBe(401);
  await stranger.close();
});

test("a Brief streams live stages through the rewrite and COMPLETE leads to the Report", async ({ page, request }) => {
  await choosePersona(page);
  await control(request, "gate/close"); // hold research so a live stage can be seen
  await submitBrief(page, BRIEF);

  await expectCurrentStep(page, "Researching");
  await expect(page.getByRole("heading", { level: 1 })).toHaveText("Researching…");
  await expect(rail(page).locator(".rail__step--done")).toContainText("Planning");

  await control(request, "gate/open");
  await expect(page.getByRole("heading", { level: 1 })).toHaveText("Your Report is ready", { timeout: 60_000 });
  await expect(rail(page).locator(".rail__step--done")).toHaveCount(7);
  await page.getByRole("link", { name: "Read the Report" }).click();
  await expect(page).toHaveURL(/\/reports\/rep_/);
  await expect(page.locator(".claim").first()).toBeVisible();
});

test("a stream the server closes reconnects with Last-Event-ID; progress stays and is not FAILED", async ({ page, request }) => {
  const streams: { lastEventId: string | undefined }[] = [];
  page.on("request", async (r) => {
    if (r.url().includes("/api/research/status")) {
      streams.push({ lastEventId: (await r.allHeaders())["last-event-id"] });
    }
  });
  await choosePersona(page);
  await control(request, "gate/close");
  await submitBrief(page, BRIEF);
  await expectCurrentStep(page, "Researching");

  // The backend ends an unfinished stream after 3 s; EventSource reconnects by itself.
  await expect.poll(() => streams.length, { timeout: 20_000 }).toBeGreaterThanOrEqual(3);
  expect(streams[0].lastEventId).toBeUndefined();
  expect(Number(streams[1].lastEventId)).toBeGreaterThan(0);

  await expectCurrentStep(page, "Researching");
  await expect(page.getByTestId("run-failed")).toHaveCount(0);
  await expect(rail(page).getByText("Researching", { exact: true })).toHaveCount(1);
  await expect(rail(page).getByText("Planning", { exact: true })).toHaveCount(1);

  await control(request, "gate/open");
  await expect(page.getByTestId("run-complete")).toBeVisible({ timeout: 60_000 });
  await expect(rail(page).locator("li")).toHaveCount(7); // no step drawn twice by the replay
});

test("reloading an active Run rebuilds its progress from the replayed stream", async ({ page, request }) => {
  await choosePersona(page);
  await control(request, "gate/close");
  const runId = await submitBrief(page, BRIEF);
  await expectCurrentStep(page, "Researching");

  await page.reload();
  await expect(page).toHaveURL(new RegExp(`/runs/${runId}$`));
  await expectCurrentStep(page, "Researching");
  await expect(rail(page).locator(".rail__step--done")).toContainText("Planning");

  await control(request, "gate/open");
  await expect(page.getByTestId("run-complete")).toBeVisible({ timeout: 60_000 });
});

// Clarification tests run as Bob: the scripted Planner asks whenever the Vague Brief appears
// in its prompt, and that includes earlier Run summaries, so Alice's history must not hold it.
test("a clarifying question is answered and the Run resumes to its Report", async ({ page }) => {
  await choosePersona(page, "bob");
  await submitBrief(page, VAGUE);
  await expect(page.getByText(QUESTION)).toBeVisible();
  await expect(page.getByRole("heading", { level: 1 })).toHaveText("Waiting for your answer");

  await page.getByLabel("Your answer").fill("Electric vehicles in China and the US, 2025: BYD and Tesla.");
  await page.getByRole("button", { name: "Send answer" }).click();
  await expect(page.getByTestId("run-complete")).toBeVisible({ timeout: 60_000 });
  await expect(page.getByText("Your answer was used for this research.")).toBeVisible();
  await expect(page.getByTestId("run-failed")).toHaveCount(0);
});

test("a late answer gets 409, the page reconciles, and the backend timeout lets the Run continue", async ({ page, request }) => {
  await choosePersona(page, "bob");
  const runId = await submitBrief(page, VAGUE);
  await expect(page.getByText(QUESTION)).toBeVisible();

  await control(request, "clock/advance?minutes=31"); // the server's deadline has passed
  const answered = page.waitForResponse((r) => r.url().includes(`/api/research/${runId}/clarify`));
  await page.getByLabel("Your answer").fill("Too late");
  await page.getByRole("button", { name: "Send answer" }).click();
  expect((await answered).status()).toBe(409);
  await expect(page.getByText("Your answer arrived after the question closed. Research continues without it.")).toBeVisible();
  await expect(page.getByTestId("run-failed")).toHaveCount(0);

  // The durable timeout job (APScheduler at the deadline) resolves it; the page follows.
  await control(request, `clarification/${runId}/timeout?owner=bob`);
  await expect(page.getByTestId("run-complete")).toBeVisible({ timeout: 60_000 });
  await expect(page.getByText(/arrived after the question timed out\. Research went ahead on a best-effort reading of the Brief/)).toBeVisible();
  await expect(page.getByTestId("run-failed")).toHaveCount(0);
});

test("an unanswered question times out on the server and the Run still completes", async ({ page, request }) => {
  await choosePersona(page, "bob");
  const runId = await submitBrief(page, VAGUE);
  await expect(page.getByText(QUESTION)).toBeVisible();

  await control(request, "clock/advance?minutes=31");
  await control(request, `clarification/${runId}/timeout?owner=bob`);
  await expect(page.getByTestId("run-complete")).toBeVisible({ timeout: 60_000 });
  await expect(page.getByText("The question timed out. Research went ahead on a best-effort reading of the Brief.")).toBeVisible();
  await expect(page.getByTestId("run-failed")).toHaveCount(0);
});

test("the Report's inline citations open the drawer with the API's exact Source and Passage", async ({ page }) => {
  await choosePersona(page);
  await submitBrief(page, BRIEF);
  await page.getByRole("link", { name: "Read the Report" }).click({ timeout: 60_000 });
  await page.waitForURL(/\/reports\/rep_/);
  const reportId = decodeURIComponent(new URL(page.url()).pathname.split("/").pop()!);

  const citations = await (await page.request.get(`/api/reports/${reportId}/citations`)).json();
  expect(citations.length).toBeGreaterThan(0);
  const markers = page.getByRole("button", { name: /^View source for claim \d+$/ });
  await expect(markers).toHaveCount(citations.length);

  const n = Math.min(2, citations.length);
  await page.getByRole("button", { name: `View source for claim ${n}` }).click();
  const drawer = page.getByRole("dialog", { name: `Source for claim ${n}` });
  await expect(drawer).toBeVisible();
  const expected = citations[n - 1];
  await expect(drawer.getByTestId("citation-source")).toHaveText(expected.source_title);
  await expect(drawer.getByTestId("citation-passage")).toHaveText(expected.passage_text);
  await expect(drawer.getByTestId("citation-credibility")).toContainText(
    expected.credibility_score == null ? "Not scored" : `${expected.credibility_score.toFixed(2)} of 1`);
  if (expected.published_at == null) {
    await expect(drawer.getByTestId("citation-published")).toHaveText("Not stated by the source");
  }

  await page.keyboard.press("Escape");
  await expect(drawer).toBeHidden();
  await expect(page.getByRole("button", { name: `View source for claim ${n}` })).toBeFocused();

  // T20 B1: the summary heading, one Confidence label per section as the API derives it, and
  // each Claim's Source inline; all still shown when printed (the PDF is this page printed).
  const report = await (await page.request.get(`/api/reports/${reportId}`)).json();
  expect(report.section_confidence.length).toBeGreaterThan(1);
  const first = citations[0];
  const inline = `[Source: ${first.source_domain ?? first.source_title}, ${first.published_at ? first.published_at.slice(0, 10) : "undated"}, credibility: ${first.credibility_score != null ? first.credibility_score.toFixed(2) : "not scored"}]`;
  for (const media of ["screen", "print"] as const) {
    await page.emulateMedia({ media });
    await expect(page.getByRole("heading", { level: 2, name: "Executive Summary" })).toBeVisible();
    const labels = page.getByTestId("section-confidence");
    await expect(labels).toHaveText(report.section_confidence.map((c: string | null) =>
      c === null ? "Confidence unavailable"
        : c === "BACKGROUND" ? "Background from earlier Reports · no current Confidence"
        : `Confidence: ${c}`));
    await expect(page.locator('[data-claim="0:0"] .claim__source')).toHaveText(inline);
  }
  await page.emulateMedia({ media: "screen" });
});

test("an estimate is shown, yet the backend can still refuse the Run with 422", async ({ page }) => {
  await choosePersona(page, "carol"); // no earlier Reports, so memory is unavailable
  await page.getByLabel("Brief", { exact: true }).fill(BRIEF);
  await page.getByRole("checkbox", { name: /^Web/ }).setChecked(false);
  await page.getByRole("checkbox", { name: /^Your documents/ }).setChecked(false);
  await page.getByRole("checkbox", { name: /^Earlier Reports/ }).setChecked(true);
  await expect(page.getByTestId("estimate-value")).toHaveText("about 45 sec");

  const submitted = page.waitForResponse((r) => r.url().endsWith("/api/research/run"));
  await page.getByRole("button", { name: "Start research" }).click();
  expect((await submitted).status()).toBe(422);
  await expect(page.locator(".notice--error")).toContainText("This Brief can't be researched as submitted");
  await expect(page).toHaveURL(/\/$/);
});

// --- switching persona is a hard boundary --------------------------------------------------

const PDF = { name: "deck.pdf", mimeType: "application/pdf", buffer: Buffer.from("%PDF-1.7\n%stub\n") };

test("switching persona on the start page drops the Brief and attached PDFs", async ({ page }) => {
  await choosePersona(page, "alice");
  await page.getByLabel("Brief", { exact: true }).fill("Alice's unfinished Brief");
  await page.getByLabel("Documents", { exact: true }).setInputFiles(PDF);
  const documents = page.getByRole("checkbox", { name: /^Your documents/ });
  await expect(documents).toBeChecked();
  await expect(documents).toBeDisabled(); // held on while a PDF is attached
  await expect(page.getByText("deck.pdf")).toBeVisible();

  await page.getByLabel("Researching as").selectOption("bob");
  await expect(page.getByLabel("Researching as")).toHaveValue("bob");
  await expect(page.getByLabel("Brief", { exact: true })).toHaveValue("");
  await expect(page.getByText("deck.pdf")).toHaveCount(0);
  await expect(documents).not.toBeChecked(); // Bob's default
});

test("switching persona during an active Run leaves the Run and stops following it", async ({ page, request }) => {
  const streams: string[] = [];
  page.on("request", (r) => {
    if (r.url().includes("/api/research/status")) streams.push(r.url());
  });
  await choosePersona(page, "alice");
  await control(request, "gate/close");
  await submitBrief(page, BRIEF);
  await expectCurrentStep(page, "Researching");

  await page.getByLabel("Researching as").selectOption("bob");
  await page.waitForURL((url) => url.pathname === "/");
  await expect(page.getByLabel("Researching as")).toHaveValue("bob");
  await expect(page.getByRole("list", { name: "Run progress" })).toHaveCount(0);
  const after = streams.length;
  await page.waitForTimeout(5_000); // longer than a stream's lifetime: no reconnect follows
  expect(streams.length).toBe(after);
});

test("switching persona on a Report page leaves that Report", async ({ page }) => {
  await choosePersona(page, "alice");
  await submitBrief(page, BRIEF);
  await page.getByRole("link", { name: "Read the Report" }).click({ timeout: 60_000 });
  await page.waitForURL(/\/reports\/rep_/);
  await expect(page.locator(".claim").first()).toBeVisible();

  await page.getByLabel("Researching as").selectOption("carol");
  await page.waitForURL((url) => url.pathname === "/");
  await expect(page.getByLabel("Researching as")).toHaveValue("carol");
  await expect(page.locator(".claim")).toHaveCount(0);
});

test("choosing a first persona on a Report deep link reloads that Report as the persona", async ({ browser, request }) => {
  // A Report of Alice's, found (or made) through the API with its own cookie jar.
  expect((await request.post("/api/session", { data: { persona_id: "alice" } })).ok()).toBeTruthy();
  let reports = await (await request.get("/api/reports")).json();
  if (reports.length === 0) {
    const run = await (await request.post("/api/research/run", { data: { brief: BRIEF, source_toggles: { web: true } } })).json();
    await expect.poll(async () => {
      reports = await (await request.get("/api/reports")).json();
      return reports.some((r: { run_id: string }) => r.run_id === run.run_id);
    }, { timeout: 60_000 }).toBe(true);
  }
  const reportId: string = reports[0].id;

  const fresh = await browser.newContext(); // no persona cookie
  const page = await fresh.newPage();
  await page.goto(`/reports/${reportId}`);
  await expect(page.getByRole("heading", { name: "The Report could not be shown" })).toBeVisible();

  await page.getByLabel("Researching as").selectOption("alice");
  await page.waitForURL((url) => url.pathname === `/reports/${reportId}`);
  await expect(page.locator(".claim").first()).toBeVisible();
  await expect(page.getByLabel("Researching as")).toHaveValue("alice");
  await fresh.close();
});

test("a one-shot Brief stops at 500 characters in the browser and at the API", async ({ page, request }) => {
  await choosePersona(page);
  const brief = page.getByLabel("Brief", { exact: true });
  await expect(page.getByTestId("brief-count")).toHaveText("0/500");
  await brief.focus();
  await page.keyboard.insertText(`${BRIEF} ${"x".repeat(600)}`); // as a paste: one input event
  await expect(page.getByTestId("brief-count")).toHaveText("500/500");
  expect(await brief.evaluate((el: HTMLTextAreaElement) => Array.from(el.value).length)).toBe(500);
  await expect(page.getByRole("status").filter({ hasText: "Text beyond the limit was not added." })).toHaveCount(1);
  await page.keyboard.type("y");
  await expect(page.getByTestId("brief-count")).toHaveText("500/500");
  // A paste in the middle that starts like the text after it keeps that text and the caret.
  await brief.fill("Start End");
  await brief.evaluate((el: HTMLTextAreaElement) => el.setSelectionRange(6, 6));
  await page.keyboard.insertText(`E${"x".repeat(600)}`);
  await expect(brief).toHaveValue(`Start E${"x".repeat(490)}End`);
  expect(await brief.evaluate((el: HTMLTextAreaElement) => el.selectionStart)).toBe(497);
  await brief.fill("\u{1F697}".repeat(500)); // 1000 UTF-16 code units, 500 characters
  await expect(page.getByTestId("brief-count")).toHaveText("500/500");

  // The server counts the same way (that no Run is created is tests/api/test_t20_brief_limit.py).
  expect((await request.post("/api/session", { data: { persona_id: "alice" } })).ok()).toBeTruthy();
  for (const over of ["a".repeat(501), "\u{1F697}".repeat(501)]) {
    const refused = await request.post("/api/research/run", { data: { brief: over } });
    expect(refused.status()).toBe(422);
    expect((await refused.json()).detail[0].type).toBe("string_too_long");
  }
});
