// SPDX-License-Identifier: GPL-3.0-or-later
// Copyright (C) 2026 Brad Anderson
//
// Loads a darner-web page at a phone's width and prints, as JSON, whether each
// given string is shown inside the timeline pane it's in, where it can be seen.
// tests/test_web_browser.py runs it against a fake API and asserts on that.
//
//   node tests/browser/wrap.mjs <url> <string>...
import { chromium } from "playwright";

const [url, ...needles] = process.argv.slice(2);
const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 390, height: 800 } });
await page.goto(url, { waitUntil: "networkidle" });
const result = await page.evaluate((needles) => {
  const found = {};
  const walker = document.createTreeWalker(document.querySelector("main"), NodeFilter.SHOW_TEXT);
  for (let node; (node = walker.nextNode());) {
    for (const needle of needles) {
      const at = node.data.indexOf(needle);
      if (at < 0 || needle in found) continue;
      const range = document.createRange();
      range.setStart(node, at);
      range.setEnd(node, at + needle.length);
      const text = range.getBoundingClientRect();
      const pane = node.parentElement.closest(".event").getBoundingClientRect();
      found[needle] = text.left >= pane.left && text.right <= pane.right && text.width > 0;
    }
  }
  return found;
}, needles);
await browser.close();
console.log(JSON.stringify(result));
