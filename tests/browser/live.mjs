// SPDX-License-Identifier: GPL-3.0-or-later
// Copyright (C) 2026 Brad Anderson
//
// Watches a session's page in Chromium as the session changes, on a fake
// clock. Each step prints its name and waits for a line on stdin, once
// tests/test_web_browser.py has changed the fake API to match. Prints what
// happened as JSON, last.
//
//   node tests/browser/live.mjs http://127.0.0.1:<port>/sessions/dn-aaaaa
import { createInterface } from "node:readline";
import { chromium } from "playwright";

const url = process.argv[2];
const lines = createInterface({ input: process.stdin })[Symbol.asyncIterator]();
async function step(name) {
  console.log(name);
  await lines.next();
}

const browser = await chromium.launch();
const page = await browser.newPage();
const result = { errors: [] };
page.on("pageerror", (e) => result.errors.push(e.message));
await page.clock.install();
await page.goto(url, { waitUntil: "networkidle" });
const seen = () => page.evaluate(() => {
  const latest = document.querySelector(".latest");
  return {
    seq: latest.dataset.seq, fresh: latest.classList.contains("fresh"), new: !latest.querySelector(".new").hidden,
    when: latest.querySelector(".when").textContent, used: document.querySelector("[data-live]").textContent,
    same_page: window.__same === true,
  };
});
await page.evaluate(() => { window.__same = true; });
result.loaded = await seen();

await step("usage");
await page.clock.runFor(5000);
await page.waitForFunction(() => document.querySelector("[data-live]").textContent !== "nothing yet");
result.used = await seen();

await page.clock.runFor(60000);
result.minute_later = await seen();

await step("output");
const reloaded = page.waitForEvent("load");
await page.clock.runFor(5000);
await reloaded;
result.output = await seen();
// Another of the page's reloads, for something other than news, leaves it new.
await Promise.all([page.waitForEvent("load"), page.evaluate(() => reload())]);
result.reloaded = await seen();
// Left and come back to, it isn't.
await page.goto(new URL("/", url).href, { waitUntil: "networkidle" });
await page.goto(url, { waitUntil: "networkidle" });
result.returned = await seen();

// Nor reloaded by hand.
await step("again");
const again = page.waitForEvent("load");
await page.clock.runFor(5000);
await again;
result.again = await seen();
await page.reload({ waitUntil: "networkidle" });
result.by_hand = await seen();

await browser.close();
console.log(JSON.stringify(result));
process.exit(0);
