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

function show(near, message, ok = false) {
  const alert = near.closest("main").querySelector(".alert");
  alert.textContent = message;
  alert.classList.toggle("ok", ok);
  alert.hidden = false;
}

// Sends one action. Resolves "moved" if it worked and said where to go next, which
// it has started going to, true if it worked otherwise, and false if it didn't.
async function send(url, init, near) {
  near.closest("main").querySelector(".alert").hidden = true;
  try {
    const response = await fetch(url, { method: "POST", ...init, headers: { [REQUEST_HEADER]: "1", ...init.headers } });
    const body = await response.json().catch(() => ({}));
    if (response.ok) {
      if (!body.location) return true;
      location.assign(body.location);
      return "moved";
    }
    show(near, body.error || `Failed: ${response.status}`);
  } catch (e) {
    show(near, `Failed: ${e.message}`);
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

// A slow action says it's still going, and for how long, until it's answered.
function busy(button) {
  const label = button.textContent;
  const status = button.form.querySelector(".status");
  const started = Date.now();
  button.classList.add("busy");
  button.textContent = button.dataset.busy;
  const tick = () => {
    status.textContent = `${status.dataset.text} ${Math.round((Date.now() - started) / 1000)}s so far.`;
  };
  tick();
  status.hidden = false;
  const timer = setInterval(tick, 1000);
  return () => {
    clearInterval(timer);
    button.classList.remove("busy");
    button.textContent = label;
    status.hidden = true;
  };
}

for (const form of document.querySelectorAll("form[data-action]")) {
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const button = form.querySelector("button[type=submit]");
    button.disabled = true;
    const done = button.dataset.busy ? busy(button) : () => {};
    const json = { "Content-Type": "application/json" };
    const sent = await send(form.dataset.action, { headers: json, body: JSON.stringify(fields(form)) }, form);
    if (sent) form.reset();
    if (sent === true) location.reload();
    if (!sent) {
      done();
      button.disabled = false;
    }
  });
}

// A session's history: folded, with what was unfolded kept across the page's reloads.
for (const button of document.querySelectorAll("button[data-expand]")) {
  const list = document.querySelector(button.dataset.expand);
  const all = [...list.querySelectorAll("details[data-seq]")];
  const key = `open:${location.pathname}`;
  const opened = new Set(JSON.parse(sessionStorage.getItem(key) ?? "[]"));
  const label = () => {
    button.textContent = all.every((d) => d.open) ? "Collapse all" : "Expand all";
  };
  for (const d of all) {
    d.open = opened.has(d.dataset.seq);
    d.addEventListener("toggle", () => {
      sessionStorage.setItem(key, JSON.stringify(all.filter((x) => x.open).map((x) => x.dataset.seq)));
      label();
    });
  }
  label();
  button.addEventListener("click", () => {
    const open = !all.every((d) => d.open);
    for (const d of all) d.open = open;
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
      show(form, `Uploaded ${file.name}.`, true);
    }
    button.disabled = false;
  });
}

// The new-task form. The agent fields are changes to what the task gets from its
// config sets, or else from the defaults, and offer only what fits the harness and
// backend above them; a change above that a choice no longer fits sets it back to
// No change. Above Start, what Start launches, as the API would build it.
const agent = document.querySelector("fieldset[data-catalog]");
if (agent) {
  const form = agent.closest("form");
  const catalog = JSON.parse(agent.dataset.catalog);
  const field = (id) => form.querySelector(`#${id}`);
  const [harness, backend, model, models, effort, contextWindow, network] =
    ["harness", "backend", "model", "models", "effort", "context_window", "network"].map(field);
  const allEfforts = [...effort.options].slice(1).map((o) => o.value);
  const NETWORKS = { named: "only what's named", sealed: "only what's named, and sealed" };
  let fromConfigs = {};
  let pair = null;

  function offered(name) {
    const open = { backends: catalog.backends, efforts: allEfforts, context_window: true, defaults: {}, models: {} };
    return catalog.options?.[name] ?? open;
  }

  // What a field gets with No change, and where from.
  function unchanged(key, defaults) {
    if (fromConfigs[key] != null) return [String(fromConfigs[key]), "from config sets"];
    if (defaults[key] != null) return [String(defaults[key]), "default"];
    return [null, null];
  }

  const noChange = ([value, source]) => (value ? `No change: ${value}, ${source}` : "No change");

  function fill(select, values, label) {
    const chosen = select.value;
    select.replaceChildren(new Option(label, ""), ...values.map((v) => new Option(v)));
    select.value = values.includes(chosen) ? chosen : "";
  }

  // The harness and backend the task ends up with, and what that harness offers.
  function current() {
    const name = harness.value || unchanged("harness", { harness: catalog.default_harness })[0];
    const o = offered(name);
    return { name, o, backend: backend.value || unchanged("backend", o.defaults)[0] };
  }

  function update() {
    harness.options[0].textContent = noChange(unchanged("harness", { harness: catalog.default_harness }));
    let { o } = current();
    fill(backend, o.backends, noChange(unchanged("backend", o.defaults)));
    fill(effort, o.efforts ?? allEfforts, noChange(unchanged("effort", o.defaults)));
    const now = current();
    o = now.o;
    const listed = o.models[now.backend] ?? [];
    models.replaceChildren(...listed.map((m) => new Option(m.context_window ? `${m.context_window} tokens` : "", m.id)));
    model.placeholder = noChange(unchanged("model", o.defaults));
    if (pair !== null && pair !== `${now.name} ${now.backend}` && model.value && !listed.some((m) => m.id === model.value)) {
      model.value = "";
      contextWindow.value = "";
    }
    pair = `${now.name} ${now.backend}`;
    contextWindow.disabled = !o.context_window;
    if (!o.context_window) contextWindow.value = "";
    contextWindow.placeholder = o.context_window ? noChange(unchanged("context_window", o.defaults)) : "not taken by this harness";
    const reach = fromConfigs.network ? [NETWORKS[fromConfigs.network], "from config sets"] : ["dejima's own allowlist too", "default"];
    network.options[0].textContent = noChange(reach);
  }

  function row(list, name, value, source) {
    const dd = document.createElement("dd");
    dd.textContent = value;
    if (source) {
      const from = document.createElement("span");
      from.className = "quiet";
      from.textContent = source;
      dd.append(from);
    }
    const dt = document.createElement("dt");
    dt.textContent = name;
    list.append(dt, dd);
  }

  function showPreview(preview, error) {
    const list = field("preview");
    const blurbs = field("preview-blurbs");
    const refusal = form.querySelector(".preview .refusal");
    list.replaceChildren();
    blurbs.replaceChildren();
    refusal.hidden = !error;
    refusal.textContent = error ? `Start would be refused: ${error}` : "";
    if (!preview) return;
    const set = preview.settings;
    const name = set.harness ?? catalog.default_harness;
    const o = offered(name);
    const setting = (key, input) => {
      if (set[key] != null) return [String(set[key]), input.value ? "set here" : "from config sets"];
      const fallback = key === "harness" ? catalog.default_harness : o.defaults[key];
      return fallback != null ? [String(fallback), "default"] : ["dejima decides", null];
    };
    if (preview.name) row(list, "Name", `dn-${preview.name}`, null);
    row(list, "Harness", ...setting("harness", harness));
    row(list, "Backend", ...setting("backend", backend));
    row(list, "Model", ...setting("model", model));
    row(list, "Effort", ...setting("effort", effort));
    if (o.context_window) row(list, "Context window", ...setting("context_window", contextWindow));
    row(list, "Reach", set.network ? NETWORKS[set.network] : "dejima's own allowlist too", set.network ? (network.value ? "set here" : "from config sets") : null);
    for (const [label, values] of [["Domains", preview.domains], ["Internal domains", preview.internal_domains],
      ["Dependencies", preview.dependencies], ["Files", preview.files]]) {
      row(list, label, values.length ? values.join(", ") : "none", null);
    }
    for (const blurb of preview.blurbs) {
      const details = document.createElement("details");
      const summary = document.createElement("summary");
      summary.textContent = `Blurb: ${blurb.name}`;
      const text = document.createElement("pre");
      text.textContent = blurb.text;
      details.append(summary, text);
      blurbs.append(details);
    }
  }

  let timer;
  let asked = 0;
  async function preview() {
    const n = ++asked;
    try {
      const response = await fetch("/tasks/preview", {
        method: "POST", headers: { [REQUEST_HEADER]: "1", "Content-Type": "application/json" }, body: JSON.stringify(fields(form)),
      });
      const body = await response.json().catch(() => ({}));
      if (n !== asked) return;
      if (!response.ok) return showPreview(null, body.error || `Failed: ${response.status}`);
      fromConfigs = body.from_configs;
      const before = JSON.stringify(fields(form));
      update();
      // A config set's harness can leave a change below it that no longer fits.
      if (JSON.stringify(fields(form)) !== before) return later();
      showPreview(body);
    } catch (e) {
      if (n === asked) showPreview(null, `couldn't check it: ${e.message}`);
    }
  }

  function later() {
    clearTimeout(timer);
    timer = setTimeout(preview, 250);
  }

  form.addEventListener("input", (event) => {
    if (event.target === model) {
      const found = (current().o.models[current().backend] ?? []).find((m) => m.id === model.value);
      if (found?.context_window && !contextWindow.disabled) contextWindow.value = found.context_window;
    }
    update();
    later();
  });
  update();
  preview();
}
