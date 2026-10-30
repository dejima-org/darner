// SPDX-License-Identifier: GPL-3.0-or-later
// Copyright (C) 2026 Brad Anderson
//
// Drives darner-web's actions in Chromium: a message from a session's page,
// then a task from the new-task page. Prints what happened as JSON.
// tests/test_web_browser.py runs it against a fake API and asserts on that.
//
//   node tests/browser/actions.mjs http://127.0.0.1:<port>
import { chromium } from "playwright";

const origin = process.argv[2];
const browser = await chromium.launch();
const page = await browser.newPage();
const result = { violations: [], errors: [] };
page.on("pageerror", (e) => result.errors.push(e.message));
await page.addInitScript(() => {
  window.__violations = [];
  document.addEventListener("securitypolicyviolation", (e) => window.__violations.push(`${e.violatedDirective} ${e.blockedURI}`));
});

await page.goto(`${origin}/sessions/dn-aaaaa`, { waitUntil: "networkidle" });
await page.fill("#message", "use postgres");
await Promise.all([page.waitForEvent("load"), page.click("form[data-action] button[type=submit]")]);
result.violations.push(...(await page.evaluate(() => window.__violations)));

await page.goto(`${origin}/new`, { waitUntil: "networkidle" });
await page.fill("#task", "add this recipe");
await page.check('input[name="blurbs"][value="mealie"]');
await page.fill("#domains", "allrecipes.example.org");
await Promise.all([page.waitForURL(`${origin}/sessions/dn-ccccc`), page.click("form[data-action] button[type=submit]")]);
result.landed = page.url();
result.violations.push(...(await page.evaluate(() => window.__violations)));
await browser.close();
console.log(JSON.stringify(result));
