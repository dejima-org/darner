// SPDX-License-Identifier: GPL-3.0-or-later
// Copyright (C) 2026 Brad Anderson
//
// No HTML is inserted from here: a page reloads when what it shows has
// changed, and errors are set as text. That keeps every HTML sink out of
// reach, which the CSP's Trusted Types rule then enforces.
"use strict";

const REQUEST_HEADER = "X-Darner-Request";

// Someone is typing, or has typed something not yet sent, which a reload would lose.
function typing() {
  const field = document.activeElement;
  if (field && (field.tagName === "TEXTAREA" || field.tagName === "INPUT" || field.tagName === "SELECT")) return true;
  return [...document.querySelectorAll("textarea, input:not([type=hidden]):not([type=checkbox])")].some((f) => f.value);
}

function showError(near, message) {
  const error = near.closest("main").querySelector(".error");
  error.textContent = message;
  error.hidden = false;
}

// Sends one action. Resolves "moved" if it worked and said where to go next, which
// it has started going to, true if it worked otherwise, and false if it didn't.
async function send(url, init, near) {
  near.closest("main").querySelector(".error").hidden = true;
  try {
    const response = await fetch(url, { method: "POST", ...init, headers: { [REQUEST_HEADER]: "1", ...init.headers } });
    const body = await response.json().catch(() => ({}));
    if (response.ok) {
      if (!body.location) return true;
      location.assign(body.location);
      return "moved";
    }
    showError(near, body.error || `Failed: ${response.status}`);
  } catch (e) {
    showError(near, `Failed: ${e.message}`);
  }
  return false;
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
    if (button.dataset.confirm && !confirm(button.dataset.confirm)) return;
    button.disabled = true;
    const json = { "Content-Type": "application/json" };
    const sent = await send(button.dataset.post, { headers: json, body: "{}" }, button);
    if (sent === true) location.reload();
    else if (!sent) button.disabled = false;
  });
}

// A form's fields as JSON. A checkbox group is always a list.
function fields(form) {
  const data = {};
  for (const [name, value] of new FormData(form)) {
    const box = form.querySelector(`input[type=checkbox][name="${CSS.escape(name)}"]`);
    if (box) (data[name] ??= []).push(value);
    else data[name] = value;
  }
  return data;
}

for (const form of document.querySelectorAll("form[data-action]")) {
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const button = form.querySelector("button[type=submit]");
    button.disabled = true;
    const json = { "Content-Type": "application/json" };
    const sent = await send(form.dataset.action, { headers: json, body: JSON.stringify(fields(form)) }, form);
    if (sent) form.reset();
    if (sent === true) location.reload();
    if (!sent) button.disabled = false;
  });
}

for (const form of document.querySelectorAll("form[data-upload]")) {
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const file = form.querySelector("input[type=file]").files[0];
    if (!file) return;
    const button = form.querySelector("button[type=submit]");
    button.disabled = true;
    const url = `${form.dataset.upload}?name=${encodeURIComponent(file.name)}`;
    if (await send(url, { headers: { "Content-Type": "application/octet-stream" }, body: file }, form)) {
      form.reset();
      showError(form, `Uploaded ${file.name}.`);
    }
    button.disabled = false;
  });
}
