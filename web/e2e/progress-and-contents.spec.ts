import { expect, test, type Page } from "@playwright/test";
import { BRIEF, choosePersona, control, expectCurrentStep, submitBrief } from "./support";

// T20 B6 in a real browser through the rewrite: per-Sub-task progress, milestones and elapsed
// time on the Run page (across a reload), and the Report's table of contents on the owner's
// Report and the public share: sticky on desktop, usable on a phone, followed by keyboard, and
// absent from print.

test.beforeEach(async ({ request }) => {
  await control(request, "gate/open");
  await control(request, "clock/reset");
});
test.afterEach(async ({ request }) => {
  await control(request, "gate/open");
});

const subtask = (page: Page, id: string) => page.getByTestId(`subtask-${id}`);

async function seconds(page: Page): Promise<number> {
  const text = (await page.getByTestId("elapsed").textContent()) ?? "";
  const m = text.match(/(?:(\d+) min )?(\d+) sec/);
  expect(m, text).not.toBeNull();
  return Number(m![1] ?? 0) * 60 + Number(m![2]);
}

test("Sub-tasks run only once dispatched, survive a reload, and finish with their outcome; elapsed time freezes", async ({ page, request }) => {
  await choosePersona(page);
  await control(request, "gate/close"); // every page fetch waits: dispatched Sub-tasks stay running
  const runId = await submitBrief(page, BRIEF);
  await expectCurrentStep(page, "Researching");
  for (const id of ["s1", "s2", "s3"]) await expect(subtask(page, id)).toContainText("Running");
  await expect(page.getByTestId("subtask-count")).toHaveText("Sub-tasks: 0 of 3 finished");
  await expect(page.getByRole("progressbar", { name: "Workflow milestones passed" })).toHaveAttribute("aria-valuenow", "1");
  await expect(page.getByTestId("milestones")).toContainText("not a time estimate");
  await expect(page.getByTestId("elapsed")).toContainText(/^Elapsed \d/);
  const before = await seconds(page);
  await expect.poll(() => seconds(page), { timeout: 10_000 }).toBeGreaterThan(before); // it ticks

  await page.reload(); // rebuilt from the replayed stream and the stored creation time
  await expect(page).toHaveURL(new RegExp(`/runs/${runId}$`));
  for (const id of ["s1", "s2", "s3"]) await expect(subtask(page, id)).toContainText("Running");
  expect(await seconds(page)).toBeGreaterThanOrEqual(before);
  await expect(page.getByRole("log", { name: "Run activity" })).toContainText("Plan accepted: 3 Sub-tasks");

  await control(request, "gate/open");
  await expect(page.getByTestId("run-complete")).toBeVisible({ timeout: 60_000 });
  for (const id of ["s1", "s2", "s3"]) await expect(subtask(page, id)).toContainText("Done");
  await expect(page.getByTestId("subtask-count")).toHaveText("Sub-tasks: 3 of 3 finished (3 done, 0 failed)");
  await expect(page.getByRole("progressbar", { name: "Workflow milestones passed" })).toHaveAttribute("aria-valuenow", "7");
  await expect(page.getByTestId("elapsed")).toContainText(/^Took /);
  const total = await page.getByTestId("elapsed").textContent();
  await page.waitForTimeout(2_000);
  await expect(page.getByTestId("elapsed")).toHaveText(total!); // frozen
  await page.reload();
  await expect(page.getByTestId("elapsed")).toHaveText(total!); // the same after a reload
});

async function aReport(page: Page): Promise<string> {
  await choosePersona(page);
  await submitBrief(page, BRIEF);
  await page.getByRole("link", { name: "Read the Report" }).click({ timeout: 60_000 });
  await page.waitForURL(/\/reports\/rep_/);
  return decodeURIComponent(new URL(page.url()).pathname.split("/").pop()!);
}

const contents = (page: Page) => page.getByRole("navigation", { name: "Report contents" });

test("the Report's contents stay in view on desktop, follow the keyboard and never print", async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 720 });
  await aReport(page);
  const nav = contents(page);
  await expect(nav).toBeVisible();
  const links = nav.getByRole("link");
  const labels = await links.allTextContents();
  expect(labels[0]).toBe("Executive Summary");
  const hrefs = await links.evaluateAll((as) => as.map((a) => a.getAttribute("href")));
  expect(new Set(hrefs).size).toBe(hrefs.length);
  for (const href of hrefs) await expect(page.locator(href!)).toHaveCount(1);
  expect(await nav.evaluate((el) => getComputedStyle(el).position)).toBe("sticky");

  // Keyboard: focus the last entry and follow it; focus and the URL move to that heading.
  const last = links.last();
  await last.focus();
  await page.keyboard.press("Enter");
  const target = hrefs.at(-1)!;
  await expect(page.locator(target)).toBeFocused();
  await expect(page).toHaveURL(new RegExp(`${target}$`));
  await expect(page.locator(target)).toBeInViewport();
  const box = await nav.boundingBox();
  expect(box!.y).toBeGreaterThanOrEqual(0); // still in view after scrolling down
  expect(box!.y).toBeLessThan(720);

  await page.emulateMedia({ media: "print" });
  await expect(nav).toBeHidden();
  await expect(page.locator("article.report")).toBeVisible();
  await expect(page.locator(".report__print-sources")).toBeVisible();
  await page.emulateMedia({ media: "screen" });
  await expect(nav).toBeVisible();
});

test("on a phone the contents sit above the Report, without horizontal scrolling", async ({ page }) => {
  await aReport(page);
  await page.setViewportSize({ width: 390, height: 844 });
  const nav = contents(page);
  await expect(nav).toBeVisible();
  expect(await nav.evaluate((el) => getComputedStyle(el).position)).toBe("static");
  const navBox = (await nav.boundingBox())!;
  const articleBox = (await page.locator("article.report").boundingBox())!;
  expect(navBox.y + navBox.height).toBeLessThanOrEqual(articleBox.y + 1);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth)).toBe(true);
  await nav.getByRole("link").nth(1).click();
  const href = await nav.getByRole("link").nth(1).getAttribute("href");
  await expect(page.locator(href!)).toBeInViewport();
});

test("the public share has its own contents, including Web Sources, and never prints them", async ({ page, browser }) => {
  const reportId = await aReport(page);
  const share = await (await page.request.post(`/api/reports/${encodeURIComponent(reportId)}/shares`)).json();
  const visitor = await browser.newContext(); // no persona: the public link alone
  const publicPage = await visitor.newPage();
  await publicPage.goto(share.path);
  const nav = contents(publicPage);
  await expect(nav).toBeVisible();
  await expect(nav.getByRole("link").last()).toHaveText("Web Sources");
  await nav.getByRole("link").last().click();
  await expect(publicPage.locator("#web-sources")).toBeFocused();
  await publicPage.emulateMedia({ media: "print" });
  await expect(nav).toBeHidden();
  await expect(publicPage.locator("article.report")).toBeVisible();
  await visitor.close();
});
