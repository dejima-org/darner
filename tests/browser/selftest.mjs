// SPDX-License-Identifier: GPL-3.0-or-later
// Copyright (C) 2026 Brad Anderson
//
// Loads darner-web's page for a session showing the self-test, in Chromium,
// and prints what a hostile agent's text managed to do there as JSON.
// tests/test_web_browser.py runs it against a fake API and asserts on that.
//
//   node tests/browser/selftest.mjs http://127.0.0.1:<port>/sessions/dn-aaaaa
import { chromium } from "playwright";

const url = process.argv[2];
const browser = await chromium.launch();
const page = await browser.newPage();
const result = { dialogs: [], hostile_requests: [], own_requests: [], violations: [], console_errors: [] };

page.on("dialog", async (dialog) => { result.dialogs.push(dialog.message()); await dialog.dismiss(); });
page.on("request", (request) => {
  const host = new URL(request.url()).hostname;
  (host.endsWith("selftest.invalid") ? result.hostile_requests : result.own_requests).push(request.url());
});
page.on("console", (message) => { if (message.type() === "error") result.console_errors.push(message.text()); });
await page.addInitScript(() => {
  window.__violations = [];
  document.addEventListener("securitypolicyviolation", (e) => window.__violations.push(`${e.violatedDirective} ${e.blockedURI}`));
});

const response = await page.goto(url, { waitUntil: "networkidle" });
result.status = response.status();
result.violations = await page.evaluate(() => window.__violations);
result.text = await page.locator("main").innerText();
result.bold_test_line = await page.locator("main .agent b, main .agent strong").count();
result.images = await page.locator("main img").count();
await browser.close();
console.log(JSON.stringify(result));
