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

// Stopping says it's still going until the API answers.
page.once("dialog", (dialog) => dialog.accept());
await page.route("**/stop", async (route) => { await new Promise((r) => setTimeout(r, 1500)); await route.continue(); });
const reloaded = page.waitForEvent("load");
await page.click("button[data-post$='/stop']");
await page.waitForFunction(() => !document.querySelector(".panel .status").hidden);
result.stopping = { button: await page.textContent("button[data-post$='/stop']"), status: await page.textContent(".panel .status") };
await reloaded;

await page.goto(`${origin}/new`, { waitUntil: "networkidle" });
await page.fill("#task", "add this recipe");
await page.check('input[name="blurbs"][value="mealie"]');
await page.fill("#domains", "allrecipes.example.org");
const offered = (id) => page.$$eval(`#${id} option`, (os) => os.map((o) => o.value));
const shown = () => page.$$eval("#preview dt", (dts) => Object.fromEntries(dts.map((dt) => [dt.textContent, dt.nextElementSibling.textContent])));
const firstLabel = (id) => page.$eval(`#${id} option`, (o) => o.textContent);
await page.waitForFunction(() => document.querySelector("#preview dt"));
result.preview = { before: await shown(), harness: await firstLabel("harness") };
await page.check('input[name="configs"][value="recipes"]');
await page.waitForFunction(() => document.querySelector("#preview dd")?.textContent.startsWith("cline"));
result.preview.recipes = {
  shown: await shown(), harness: await firstLabel("harness"), backends: await offered("backend"),
  context_window: await page.getAttribute("#context_window", "placeholder"),
  blurbs: await page.$$eval("#preview-blurbs details", (ds) => ds.map((d) => [d.querySelector("summary").textContent, d.open])),
};
await page.uncheck('input[name="configs"][value="recipes"]');
await page.check('input[name="configs"][value="local"]');
await page.waitForFunction(() => !document.querySelector(".preview .refusal").hidden);
result.preview.local = await page.textContent(".preview .refusal");
await page.uncheck('input[name="configs"][value="local"]');
await page.waitForFunction(() => document.querySelector("#preview dd")?.textContent.startsWith("claude-code"));
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
await page.fill("#name", "recipes");
await page.waitForFunction(() => document.querySelector("#preview dt")?.textContent === "Name");
result.named = await shown();
// A slow launch says it's still going.
await page.route("**/tasks", async (route) => { await new Promise((r) => setTimeout(r, 1500)); await route.continue(); });
const landing = page.waitForURL(`${origin}/sessions/dn-ccccc`);
await page.click("form[data-action] button[type=submit]");
await page.waitForFunction(() => !document.querySelector(".status").hidden);
result.starting = { button: await page.textContent("button[type=submit]"), status: await page.textContent(".status") };
await landing;
result.landed = page.url();
result.violations.push(...(await page.evaluate(() => window.__violations)));
await browser.close();
console.log(JSON.stringify(result));
