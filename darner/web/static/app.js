// SPDX-License-Identifier: GPL-3.0-or-later
// Copyright (C) 2026 Brad Anderson
//
// No HTML is inserted from here: a page reloads when what it shows has
// changed, and errors are set as text. That keeps every HTML sink out of
// reach, which the CSP's Trusted Types rule then enforces.
"use strict";

const REQUEST_HEADER = "X-Darner-Request";

function typing() {
  const field = document.activeElement;
  return field && (field.tagName === "TEXTAREA" || field.tagName === "INPUT");
}

const { state, version } = document.body.dataset;
if (state && version) {
  setInterval(async () => {
    if (typing() || document.hidden) return;
    try {
      const response = await fetch(state, { cache: "no-store", headers: { [REQUEST_HEADER]: "1" } });
      if (response.ok && (await response.json()).version !== version) location.reload();
    } catch {
      // Unreachable for now; the next check tries again.
    }
  }, 5000);
}

for (const button of document.querySelectorAll("button[data-post]")) {
  button.addEventListener("click", async () => {
    const error = button.closest("main").querySelector(".error");
    button.disabled = true;
    error.hidden = true;
    try {
      const response = await fetch(button.dataset.post, {
        method: "POST",
        headers: { [REQUEST_HEADER]: "1", "Content-Type": "application/json" },
        body: "{}",
      });
      const body = await response.json().catch(() => ({}));
      if (response.ok && body.location) {
        location.assign(body.location);
        return;
      }
      error.textContent = body.error || `Failed: ${response.status}`;
    } catch (e) {
      error.textContent = `Failed: ${e.message}`;
    }
    error.hidden = false;
    button.disabled = false;
  });
}
