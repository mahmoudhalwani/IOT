"use strict";
// Resident web app. No user-supplied text is ever inserted as HTML (textContent only).

const $ = (id) => document.getElementById(id);
const DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
let session = null;      // { user, csrf, cmd_key }
let pollTimer = null;

// ---- talking to the door unit ----------------------------------------------
async function api(path, options = {}) {
  const headers = new Headers(options.headers || {});
  if (session && options.method && options.method !== "GET") headers.set("X-CSRF-Token", session.csrf);
  let response;
  try {
    response = await fetch(path, { ...options, headers, credentials: "same-origin" });
  } catch {
    throw new Error("Door unit unavailable. Check your connection.");
  }
  const data = await response.json().catch(() => ({}));
  if (response.status === 401 && session) { showLogin(); }
  if (!response.ok) {
    const err = new Error(data.error || `Request failed (${response.status}).`);
    err.data = data;
    throw err;
  }
  return data;
}

const postJson = (path, body, method = "POST") =>
  api(path, { method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });

// Lock commands are signed with a key that exists only for this session, carry
// the current time and a random one-time number: the door unit refuses a command
// that is changed, old, or sent twice.
async function signedCommand(path) {
  const hex = (bytes) => Array.from(new Uint8Array(bytes), (b) => b.toString(16).padStart(2, "0")).join("");
  const keyBytes = new Uint8Array(session.cmd_key.match(/../g).map((h) => parseInt(h, 16)));
  const key = await crypto.subtle.importKey("raw", keyBytes, { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
  const stamp = String(Math.floor(Date.now() / 1000));
  const nonce = hex(crypto.getRandomValues(new Uint8Array(16)));
  const message = new TextEncoder().encode(["POST", path, stamp, nonce, ""].join("\n"));
  const signature = hex(await crypto.subtle.sign("HMAC", key, message));
  return api(path, { method: "POST",
    headers: { "X-Cmd-Time": stamp, "X-Cmd-Nonce": nonce, "X-Cmd-Signature": signature } });
}

// ---- sign in / out ----------------------------------------------------------
function showLogin() {
  session = null;
  clearInterval(pollTimer);
  $("app").hidden = true;
  $("login").hidden = false;
}

function showApp() {
  $("login").hidden = true;
  $("app").hidden = false;
  $("who").textContent = session.user;
  selectTab("door");
  clearInterval(pollTimer);
  pollTimer = setInterval(() => { if (!document.hidden && !$("tab-door").hidden) refreshDoor(); }, 2000);
}

$("login-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.target;
  const button = form.querySelector("button");
  $("login-error").textContent = "";
  button.disabled = true;
  try {
    session = await postJson("/api/login", {
      username: form.username.value, password: form.password.value, totp: form.totp.value });
    form.reset();
    $("totp-row").hidden = true;
    showApp();
  } catch (err) {
    if (err.data && err.data.needs_totp) { $("totp-row").hidden = false; form.totp.focus(); }
    $("login-error").textContent = err.message;
  } finally {
    button.disabled = false;
  }
});

$("logout").addEventListener("click", async () => {
  try { await api("/api/logout", { method: "POST" }); } catch { /* signed out either way */ }
  showLogin();
});

// ---- door -------------------------------------------------------------------
function renderDoor(door) {
  const names = { locked: "Locked", unlocked: "Unlocked", unknown: "Not confirmed" };
  $("door-card").dataset.state = door.state;
  $("door-state").textContent = names[door.state] || "Not confirmed";
  $("door-note").textContent = door.state === "unlocked"
    ? `Locks again by itself after ${door.relock_seconds} seconds.`
    : "Shown as last commanded; the prototype has no door sensor.";
}

async function refreshDoor() {
  try {
    renderDoor(await api("/api/door"));
    $("door-error").textContent = "";
  } catch (err) {
    $("door-card").dataset.state = "unknown";
    $("door-state").textContent = "Unavailable";
    $("door-error").textContent = err.message;
  }
}

async function doorCommand(action) {
  const buttons = [$("unlock"), $("lock")];
  buttons.forEach((b) => (b.disabled = true));
  $("door-error").textContent = "";
  try {
    renderDoor(await signedCommand(`/api/door/${action}`));
  } catch (err) {
    $("door-error").textContent = err.message;
    $("door-state").textContent = "Not confirmed";
    $("door-card").dataset.state = "unknown";
  } finally {
    buttons.forEach((b) => (b.disabled = false));
  }
}
$("unlock").addEventListener("click", () => doorCommand("unlock"));
$("lock").addEventListener("click", () => doorCommand("lock"));

// ---- access-time fields (shared by "add" and "edit") -------------------------
function buildAccessFields(container, access = {}) {
  container.replaceChildren($("access-fields").content.cloneNode(true));
  const days = container.querySelector(".days");
  DAYS.forEach((name, index) => {
    const label = document.createElement("label");
    const box = document.createElement("input");
    box.type = "checkbox";
    box.value = String(index);
    box.checked = Array.isArray(access.days) && access.days.includes(index);
    const text = document.createElement("span");
    text.textContent = name;
    label.append(box, text);
    days.append(label);
  });
  for (const input of container.querySelectorAll("[data-f]")) input.value = access[input.dataset.f] || "";
}

function readAccess(container) {
  const days = Array.from(container.querySelectorAll(".days input:checked"), (b) => Number(b.value));
  const access = { days: days.length ? days : null };
  for (const input of container.querySelectorAll("[data-f]")) access[input.dataset.f] = input.value || null;
  return access;
}

function describeAccess(a) {
  const parts = [];
  if (a.days) parts.push(a.days.map((d) => DAYS[d]).join(", "));
  if (a.start && a.end) parts.push(`${a.start} to ${a.end}`);
  if (a.valid_from) parts.push(`from ${a.valid_from.replace("T", " ")}`);
  if (a.valid_until) parts.push(`until ${a.valid_until.replace("T", " ")}`);
  return parts.length ? parts.join(" · ") : "Any time";
}

// ---- people -----------------------------------------------------------------
function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function personCard(p) {
  const card = el("div", "card person");
  const title = el("h3", "", p.name);
  title.append(el("span", "badge", p.role));
  if (!p.enabled) title.append(el("span", "badge off", "disabled"));
  card.append(title, el("p", "muted small", `${p.photos} photo${p.photos === 1 ? "" : "s"} · ${describeAccess(p.access)}`));
  const error = el("p", "error");
  const actions = el("div", "actions");

  const act = (label, className, handler) => {
    const button = el("button", `mini ${className}`.trim(), label);
    button.type = "button";
    button.addEventListener("click", async () => {
      error.textContent = "";
      button.disabled = true;
      try { await handler(); } catch (err) { error.textContent = err.message; } finally { button.disabled = false; }
    });
    actions.append(button);
  };

  act("Access time", "", async () => editAccess(p));
  const picker = el("input");
  picker.type = "file"; picker.accept = "image/*"; picker.hidden = true;
  picker.addEventListener("change", async () => {
    if (!picker.files.length) return;
    const body = new FormData();
    body.append("photos", picker.files[0]);
    error.textContent = "Checking photo…";
    try { await api(`/api/people/${p.id}/photos`, { method: "POST", body }); await loadPeople(); }
    catch (err) { error.textContent = err.message; }
    picker.value = "";
  });
  act("Add photo", "", async () => picker.click());
  act(p.enabled ? "Disable" : "Enable", "", async () => {
    await postJson(`/api/people/${p.id}`, { enabled: !p.enabled }, "PATCH");
    await loadPeople();
  });
  act("Remove", "danger", async () => {
    if (!confirm(`Remove ${p.name}? Their face data is deleted and they can no longer enter.`)) return;
    await api(`/api/people/${p.id}`, { method: "DELETE" });
    await loadPeople();
  });
  card.append(actions, picker, error);
  return card;
}

async function loadPeople() {
  const list = $("people-list");
  try {
    const people = await api("/api/people");
    list.replaceChildren(...(people.length ? people.map(personCard)
      : [el("p", "muted", "Nobody is enrolled yet. Add the first person below.")]));
  } catch (err) {
    list.replaceChildren(el("p", "error", err.message));
  }
}

let editing = null;
function editAccess(p) {
  editing = p;
  $("access-title").textContent = `Access time for ${p.name}`;
  $("access-error").textContent = "";
  buildAccessFields($("edit-access"), p.access);
  $("access-dialog").showModal();
}
$("access-cancel").addEventListener("click", () => $("access-dialog").close());
$("access-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  try {
    await postJson(`/api/people/${editing.id}`, { access: readAccess($("edit-access")) }, "PATCH");
    $("access-dialog").close();
    await loadPeople();
  } catch (err) {
    $("access-error").textContent = err.message;
  }
});

$("add-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.target;
  const button = form.querySelector("button[type=submit]");
  const body = new FormData();
  body.append("name", form.name.value);
  body.append("role", form.role.value);
  body.append("consent", form.consent.checked ? "true" : "false");
  for (const file of form.photos.files) body.append("photos", file);
  const access = readAccess($("add-access"));
  if (access.days) body.append("days", access.days.join(","));
  for (const key of ["start", "end", "valid_from", "valid_until"]) if (access[key]) body.append(key, access[key]);
  $("add-error").textContent = "Checking photos…";
  button.disabled = true;
  try {
    const result = await api("/api/people", { method: "POST", body });
    form.reset();
    buildAccessFields($("add-access"));
    $("add-error").textContent = result.problems.length ? `Added. Skipped: ${result.problems.join(" ")}` : "";
    if (!result.problems.length) $("add-card").open = false;
    await loadPeople();
  } catch (err) {
    $("add-error").textContent = err.message;
  } finally {
    button.disabled = false;
  }
});

// ---- log --------------------------------------------------------------------
const RESULT_TEXT = {
  granted: ["Door opened", "ok"], denied_unknown: ["Unknown visitor", "bad"],
  denied_spoof: ["Photo or screen attack", "bad"], denied_not_allowed: ["Outside allowed time", "bad"],
  fault: ["Fault", "bad"], rejected: ["Command rejected", "bad"], locked: ["Door locked", ""],
  login: ["Signed in", ""], person_added: ["Person added", ""], person_removed: ["Person removed", ""],
  person_changed: ["Person changed", ""],
};

async function loadLog() {
  const list = $("log-list");
  try {
    const events = await api("/api/events?limit=200");
    if (!events.length) { list.replaceChildren(el("p", "muted event", "Nothing has happened yet.")); return; }
    list.replaceChildren(...events.map((e) => {
      const row = el("div", "event");
      const [text, tone] = RESULT_TEXT[e.result] || [e.result, ""];
      const when = el("time", "", e.at.replace("T", " ").slice(5, 16));
      when.title = e.at;
      row.append(when, el("span", `what ${tone}`.trim(), `${text} · ${e.person}`),
                 el("span", "detail", [e.method, e.detail].filter(Boolean).join(" · ")));
      return row;
    }));
  } catch (err) {
    list.replaceChildren(el("p", "error event", err.message));
  }
}
$("log-refresh").addEventListener("click", loadLog);

// ---- tabs and start-up --------------------------------------------------------
function selectTab(name) {
  for (const tab of ["door", "people", "log"]) $(`tab-${tab}`).hidden = tab !== name;
  for (const button of document.querySelectorAll("nav button")) button.classList.toggle("active", button.dataset.tab === name);
  if (name === "door") refreshDoor();
  if (name === "people") loadPeople();
  if (name === "log") loadLog();
}
for (const button of document.querySelectorAll("nav button")) button.addEventListener("click", () => selectTab(button.dataset.tab));

buildAccessFields($("add-access"));
(async () => {
  try { session = await api("/api/session"); showApp(); } catch { showLogin(); }
})();
