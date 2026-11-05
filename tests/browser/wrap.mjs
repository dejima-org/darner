// SPDX-License-Identifier: GPL-3.0-or-later
// Copyright (C) 2026 Brad Anderson
//
// Loads a darner-web page at a phone's width and prints, as JSON, whether each
// given string is shown inside both its timeline pane and the screen, and
// whether the page scrolls sideways.
// tests/test_web_browser.py runs it against a fake API and asserts on that.
//
//   node tests/browser/wrap.mjs <url> <string>...
import { chromium } from "playwright";

const [url, ...needles] = process.argv.slice(2);
const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 390, height: 800 } });
await page.goto(url, { waitUntil: "networkidle" });
// Unfolded, so what's checked is what's on show.
if (await page.$("button[data-expand]")) await page.click("button[data-expand]");
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
      const screen = document.documentElement.clientWidth;
      found[needle] = text.width > 0 && text.left >= Math.max(pane.left, 0) && text.right <= Math.min(pane.right, screen);
    }
  }
  const root = document.documentElement;
  // The header is one line: everything in it is vertically centred on the same line.
  const middles = [...document.querySelector("nav").children].map((c) => {
    const box = c.getBoundingClientRect();
    return Math.round(box.top + box.height / 2);
  });
  return { found, scrolls_sideways: root.scrollWidth > root.clientWidth, header_lines: new Set(middles).size };
}, needles);
await browser.close();
console.log(JSON.stringify(result));
