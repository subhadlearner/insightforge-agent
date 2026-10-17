import { expect, test } from "@playwright/test";
import { choosePersona, control, WEB_PORT } from "./support";

test.beforeEach(async ({ request }) => {
  await control(request, "gate/open");
  await control(request, "clock/reset");
  await control(request, "t15/seed");
});

test("T15 Watchlist -> Diff -> Alert -> export -> public share -> revoke", async ({ page, browser }) => {
  await choosePersona(page);

  await page.goto("/watchlist");
  await expect(page.getByText("5 of 5 runs started", { exact: false })).toBeVisible();
  await expect(page.getByText(/4 completed · 1 failed/)).toBeVisible();
  await expect(page.getByText(/fixture provider unavailable/)).toBeVisible();
  await expect(page.getByText(/3 runs without reconfirmation/)).toBeVisible();
  await expect(page.getByText(/Negated: Model 3.*Autopilot/)).toBeVisible();

  await page.getByRole("link", { name: "View Diff history" }).click();
  await expect(page).toHaveURL(/\/watchlist\/wl_e2e$/);
  await expect(page.getByText(/changed · material change/i)).toBeVisible();
  await expect(page.locator(".diff-entry--material").getByText(/As of .* \(derived from source observation date\)/)).toHaveCount(2);
  await expect(page.getByText(/Changed on/)).toHaveCount(0);
  await expect(page.getByText(/Change detected on/)).toHaveCount(0);
  await expect(page.locator(".diff-entry--material").getByText("Previously established provenance:")).toBeVisible();
  await expect(page.locator(".diff-entry--material").getByText("This Run provenance:")).toBeVisible();

  await page.goto("/alerts");
  await expect(page.getByText(/Change detected on/)).toBeVisible();
  await expect(page.getByRole("link", { name: "Previous establishing Report" })).toHaveAttribute("href", "/reports/rep_t15_1");
  await page.getByRole("link", { name: "Open new Report" }).click();
  await expect(page).toHaveURL(/\/reports\/rep_t15_2$/);
  await expect(page.getByRole("heading", { name: /Model 3 pricing — 29,000 dollars/ })).toBeVisible();

  const markdownDownload = page.waitForEvent("download");
  await page.getByRole("button", { name: "Export Markdown" }).click();
  const markdown = await markdownDownload;
  expect(markdown.suggestedFilename()).toBe("rep_t15_2.md");

  const pdfDownload = page.waitForEvent("download");
  await page.getByRole("button", { name: "Export PDF" }).click();
  const pdf = await pdfDownload;
  expect(pdf.suggestedFilename()).toBe("rep_t15_2.pdf");
  const stream = await pdf.createReadStream();
  const chunks: Buffer[] = [];
  for await (const chunk of stream) chunks.push(Buffer.from(chunk));
  const pdfBytes = Buffer.concat(chunks);
  expect(pdfBytes.subarray(0, 5).toString()).toBe("%PDF-");
  expect(pdfBytes.length).toBeGreaterThan(1000);

  await page.getByRole("button", { name: "Create share link" }).click();
  const publicLink = page.getByText("Public link:").locator("..").getByRole("link");
  await expect(publicLink).toBeVisible();
  const path = await publicLink.getAttribute("href");
  expect(path).toMatch(/^\/share\//);

  const publicContext = await browser.newContext();
  const publicPage = await publicContext.newPage();
  await publicPage.goto(`http://localhost:${WEB_PORT}${path}`);
  await expect(publicPage.getByRole("heading", { name: /Model 3 pricing — 29,000 dollars/ })).toBeVisible();
  await expect(publicPage.getByRole("link", { name: "Pricing source t15_2" })).toBeVisible();
  expect((await publicContext.cookies()).some((c) => c.name === "insightforge_persona")).toBe(false);
  await expect(publicPage.getByRole("navigation", { name: "Research navigation" })).toHaveCount(0);
  await expect(publicPage.getByLabel("Researching as")).toHaveCount(0);
  await expect(publicPage.getByRole("link", { name: "Watchlist" })).toHaveCount(0);
  await expect(publicPage.getByRole("link", { name: "Alerts" })).toHaveCount(0);

  await page.getByRole("button", { name: "Revoke share link" }).click();
  await expect(page.getByText(/can no longer be opened/)).toBeVisible();
  await publicPage.reload();
  await expect(publicPage.getByRole("heading", { name: "Shared Report unavailable" })).toBeVisible();
  await publicContext.close();
});
