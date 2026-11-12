"use strict";
// DEMO ONLY. Replaces the door unit with a pretend one that lives in this
// browser tab, so the pages can be shown from static hosting (GitHub Pages).
// Nothing here talks to a real door, and nothing typed here is sent anywhere.
(() => {
  const realFetch = window.fetch.bind(window);
  const now = () => new Date();
  const stamp = (d) => new Date(d.getTime() - d.getTimezoneOffset() * 60000).toISOString().slice(0, 19);
  const ago = (minutes) => stamp(new Date(Date.now() - minutes * 60000));
  const noAccess = { days: null, start: null, end: null, valid_from: null, valid_until: null };

  const state = {
    user: sessionStorage.getItem("demo-user"),
    door: { state: "locked", since: Math.floor(Date.now() / 1000), relock_seconds: 5, confirmed: false },
    relock: null,
    nextId: 4,
    people: [
      { id: 1, name: "Mahmoud", role: "resident", enabled: true, photos: 3, access: { ...noAccess } },
      { id: 2, name: "Sara", role: "resident", enabled: true, photos: 2, access: { ...noAccess } },
      { id: 3, name: "Cleaner", role: "guest", enabled: true, photos: 1,
        access: { ...noAccess, days: [0, 3], start: "09:00", end: "12:00" } },
    ],
    events: [],
  };
  let eventId = 0;
  const log = (method, result, person, detail, at) =>
    state.events.unshift({ id: ++eventId, at: at || stamp(now()), method, result, person, detail,
                           clip: null, clip_reason: "demo" });
  log("face", "granted", "Sara", "", ago(190));
  log("face", "denied_unknown", "unknown", "", ago(121));
  log("face", "denied_spoof", "unknown", "photo or screen detected", ago(95));
  log("face", "denied_not_allowed", "Cleaner", "outside allowed hours", ago(60));
  log("face", "granted", "Mahmoud", "", ago(12));

  const reply = (body, status = 200) =>
    new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
  const fail = (status, error) => reply({ error }, status);
  const setDoor = (value) => { state.door = { ...state.door, state: value, since: Math.floor(Date.now() / 1000) }; };

  function accessFrom(get) {
    const days = get("days");
    const list = Array.isArray(days) ? days : (days ? String(days).split(",").map(Number) : null);
    const a = { days: list && list.length && list.length < 7 ? list : null,
                start: get("start") || null, end: get("end") || null,
                valid_from: get("valid_from") || null, valid_until: get("valid_until") || null };
    if ((a.start === null) !== (a.end === null)) throw new Error("give both a start and an end hour, or neither");
    return a;
  }

  async function handle(path, options) {
    const method = (options.method || "GET").toUpperCase();
    const body = options.body;
    if (path === "/api/login") {
      const data = JSON.parse(body || "{}");
      if (!data.username || !data.password) return fail(400, "Enter your username and password.");
      state.user = String(data.username).slice(0, 32);
      sessionStorage.setItem("demo-user", state.user);
      log("phone", "login", state.user, "signed in to the website");
    }
    if (!state.user) return fail(401, "Please sign in.");
    if (path === "/api/login" || path === "/api/session")
      return reply({ user: state.user, csrf: "demo", cmd_key: "00".repeat(32) });
    if (path === "/api/logout") { state.user = null; sessionStorage.removeItem("demo-user"); return reply({ ok: true }); }
    if (path === "/api/door") return reply(state.door);
    if (path === "/api/door/unlock") {
      clearTimeout(state.relock);
      setDoor("unlocked");
      state.relock = setTimeout(() => setDoor("locked"), state.door.relock_seconds * 1000);
      log("phone", "granted", state.user, "remote unlock");
      return reply(state.door);
    }
    if (path === "/api/door/lock") {
      clearTimeout(state.relock);
      setDoor("locked");
      log("phone", "locked", state.user, "remote lock");
      return reply(state.door);
    }
    if (path.startsWith("/api/events")) return reply(state.events);
    if (path === "/api/people" && method === "GET") return reply(state.people);
    if (path === "/api/people" && method === "POST") {
      const name = String(body.get("name") || "").trim();
      const photos = body.getAll("photos").length;
      if (!name) return fail(400, "Enter a name (up to 40 characters).");
      if (body.get("consent") !== "true") return fail(400, "The person must agree to face enrolment first.");
      if (!photos) return fail(400, "Add at least one photo of the person's face.");
      let access;
      try { access = accessFrom((k) => body.get(k)); } catch (e) { return fail(400, `Access time is not valid: ${e.message}`); }
      const person = { id: state.nextId++, name, role: body.get("role") === "guest" ? "guest" : "resident",
                       enabled: true, photos: Math.min(photos, 5), access };
      state.people.push(person);
      log("phone", "person_added", name, `${person.role} added by ${state.user}`);
      return reply({ person, problems: [] }, 201);
    }
    const match = path.match(/^\/api\/people\/(\d+)(\/photos)?$/);
    if (match) {
      const person = state.people.find((p) => p.id === Number(match[1]));
      if (!person) return fail(404, "That person does not exist.");
      if (match[2]) {
        if (person.photos >= 5) return fail(400, `No usable photo. ${person.name} already has 5 photos`);
        person.photos += 1;
        log("phone", "person_changed", person.name, `1 photo(s) added by ${state.user}`);
        return reply({ person, problems: [] });
      }
      if (method === "DELETE") {
        state.people = state.people.filter((p) => p !== person);
        log("phone", "person_removed", person.name, `removed by ${state.user}; face data deleted`);
        return reply({ ok: true });
      }
      const data = JSON.parse(body || "{}");
      const changes = [];
      if (data.access) {
        try { person.access = accessFrom((k) => data.access[k]); } catch (e) { return fail(400, `Access time is not valid: ${e.message}`); }
        changes.push("access time changed");
      }
      if ("enabled" in data) { person.enabled = Boolean(data.enabled); changes.push(person.enabled ? "enabled" : "disabled"); }
      if (changes.length) log("phone", "person_changed", person.name, `${changes.join(", ")} by ${state.user}`);
      return reply(person);
    }
    return fail(404, "Not found.");
  }

  window.fetch = (input, options = {}) => {
    const url = typeof input === "string" ? input : input.url;
    return url.startsWith("/api/") ? handle(url.split("?")[0], options) : realFetch(input, options);
  };

  document.addEventListener("DOMContentLoaded", () => {
    const banner = document.createElement("p");
    banner.className = "demo-banner";
    banner.textContent = "Demo page. It is not connected to a real door: the people and the log are examples, " +
                         "and any username and password will sign in.";
    document.body.prepend(banner);
  });
})();
