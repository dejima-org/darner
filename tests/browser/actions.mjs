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
const offered = (id) => page.$$eval(`#${id} option`, (os) => os.map((o) => o.value));
result.agent = { default: { backends: await offered("backend"), models: await offered("models") } };
await page.selectOption("#harness", "cline");
await page.selectOption("#backend", "self-hosted");
await page.fill("#model", "qwen3.5-4b");
result.agent.cline = {
  backends: await offered("backend"), efforts: await offered("effort"), models: await offered("models"),
  context_window: await page.inputValue("#context_window"),
};
await page.selectOption("#harness", "claude-code");
result.agent.back = {
  backend: await page.inputValue("#backend"), model: await page.inputValue("#model"),
  context_window: await page.inputValue("#context_window"), disabled: await page.isDisabled("#context_window"),
};
await page.selectOption("#harness", "cline");
await page.selectOption("#backend", "openrouter");
await page.fill("#model", "z-ai/glm-5.3");
await Promise.all([page.waitForURL(`${origin}/sessions/dn-ccccc`), page.click("form[data-action] button[type=submit]")]);
result.landed = page.url();
result.violations.push(...(await page.evaluate(() => window.__violations)));
await browser.close();
console.log(JSON.stringify(result));
