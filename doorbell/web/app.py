"""HTTP API and pages for residents.

Security model in one place:
- every /api route except login needs a valid session cookie
  (HttpOnly, Secure, SameSite=Strict, host-only);
- every request that changes something must also come from this site's own
  origin and carry the session's CSRF token;
- lock/unlock commands are additionally signed, time-limited and single-use,
  so a captured command cannot be replayed or altered (MH-10);
- the access log has no edit or delete route at all (MH-06);
- pages are served with a strict Content-Security-Policy (no inline script).
"""
from __future__ import annotations

import hmac
import json
import threading
import time
from datetime import datetime, time as dtime
from pathlib import Path
from typing import Callable, Optional

import cv2
import numpy as np
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from ..access import AccessRule
from ..config import Config
from ..door import Door
from ..enroll import EnrollError, add_face
from ..face import Gallery
from ..store import Person, Store
from .accounts import Accounts, AuthError, Session

STATIC = Path(__file__).parent / "static"
MAX_JSON = 16 * 1024
MAX_PHOTOS = 5
CSP = ("default-src 'self'; img-src 'self' blob: data:; object-src 'none'; "
       "base-uri 'none'; frame-ancestors 'none'; form-action 'self'")


class ApiError(Exception):
    def __init__(self, status: int, message: str, **extra):
        self.status, self.message, self.extra = status, message, extra


def person_json(p: Person) -> dict:
    r = p.rule
    return {"id": p.id, "name": p.name, "role": p.role, "enabled": p.enabled,
            "photos": p.template_count,
            "access": {"days": sorted(r.days) if r.days is not None else None,
                       "start": r.start.isoformat("minutes") if r.start else None,
                       "end": r.end.isoformat("minutes") if r.end else None,
                       "valid_from": r.valid_from.isoformat(timespec="minutes") if r.valid_from else None,
                       "valid_until": r.valid_until.isoformat(timespec="minutes") if r.valid_until else None}}


def rule_from(data: dict) -> AccessRule:
    """Builds an access rule from form/JSON values; empty values mean 'no limit'."""
    try:
        days = data.get("days")
        if isinstance(days, str):
            days = [int(d) for d in days.split(",") if d.strip() != ""]
        if days is not None:
            days = frozenset(int(d) for d in days)
            if not days or not days <= set(range(7)):
                raise ValueError("days must be numbers 0 (Monday) to 6 (Sunday)")
            if len(days) == 7:
                days = None
        start, end = data.get("start") or None, data.get("end") or None
        if (start is None) != (end is None):
            raise ValueError("give both a start and an end hour, or neither")
        vf, vu = data.get("valid_from") or None, data.get("valid_until") or None
        rule = AccessRule(
            valid_from=datetime.fromisoformat(vf) if vf else None,
            valid_until=datetime.fromisoformat(vu) if vu else None,
            days=days,
            start=dtime.fromisoformat(start) if start else None,
            end=dtime.fromisoformat(end) if end else None)
    except (ValueError, TypeError) as e:
        raise ApiError(400, f"Access time is not valid: {e}")
    if rule.valid_from and rule.valid_until and rule.valid_until <= rule.valid_from:
        raise ApiError(400, "Access must end after it starts.")
    return rule


def create_app(cfg: Config, store: Store, accounts: Accounts, door: Door,
               engine_factory: Callable, clock=time.time) -> Starlette:
    cookie = "session" if cfg.web_dev else "__Host-session"
    enrol_lock = threading.Lock()
    state = {"engine": None, "logins": []}

    # ---- helpers ----------------------------------------------------------
    def session_of(request: Request) -> Session:
        s = accounts.session(request.cookies.get(cookie))
        if s is None:
            raise ApiError(401, "Please sign in.")
        return s

    def writer(request: Request) -> Session:
        """For routes that change something: session + same origin + CSRF token."""
        s = session_of(request)
        origin = request.headers.get("origin", "")
        host = request.headers.get("host", "")
        if not origin or origin.split("://", 1)[-1] != host:
            raise ApiError(403, "Request did not come from this site.")
        if not hmac.compare_digest(request.headers.get("x-csrf-token", ""), s.csrf):
            raise ApiError(403, "Security token missing or wrong. Reload the page.")
        return s

    async def json_body(request: Request) -> dict:
        raw = await request.body()
        if len(raw) > MAX_JSON:
            raise ApiError(413, "Request is too large.")
        try:
            data = json.loads(raw or b"{}")
        except ValueError:
            raise ApiError(400, "Request is not valid JSON.")
        if not isinstance(data, dict):
            raise ApiError(400, "Request is not valid JSON.")
        return data

    def session_payload(s: Session) -> dict:
        return {"user": s.username, "csrf": s.csrf, "cmd_key": s.cmd_key.hex()}

    def engine():
        if state["engine"] is None:
            state["engine"] = engine_factory()
        return state["engine"]

    async def photos_from(form) -> list:
        files = [f for f in form.getlist("photos") if hasattr(f, "read")]
        if len(files) > MAX_PHOTOS:
            raise ApiError(400, f"At most {MAX_PHOTOS} photos at a time.")
        images = []
        for f in files:
            raw = await f.read()
            if len(raw) > cfg.upload_max_mb * 1024 * 1024:
                raise ApiError(413, f"{f.filename}: photo is larger than {cfg.upload_max_mb:g} MB.")
            img = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR) if raw else None
            if img is None:
                raise ApiError(400, f"{f.filename}: not a readable image.")
            longest = max(img.shape[:2])
            if longest > 1280:                    # phone photos are far larger than needed
                scale = 1280 / longest
                img = cv2.resize(img, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
            images.append((f.filename or "photo", img))
        return images

    def enrol(person_id: int, images: list) -> tuple[int, list]:
        """Runs in a worker thread. Photos are used in memory and never saved."""
        with enrol_lock:
            eng, gallery = engine(), Gallery(cfg)
            gallery.load(store.templates())
            added, problems = 0, []
            for label, img in images:
                try:
                    add_face(eng, gallery, store, cfg, person_id, img)
                    added += 1
                except EnrollError as e:
                    problems.append(f"{label}: {e}")
            return added, problems

    async def multipart(request: Request):
        limit = (MAX_PHOTOS * cfg.upload_max_mb + 1) * 1024 * 1024
        if int(request.headers.get("content-length") or 0) > limit:
            raise ApiError(413, "Upload is too large.")
        return await request.form(max_files=MAX_PHOTOS + 1, max_fields=20)

    # ---- session ----------------------------------------------------------
    async def login(request: Request):
        now = clock()
        state["logins"] = [t for t in state["logins"] if now - t < 60]
        if len(state["logins"]) >= 30:            # whole-site brake on password guessing
            raise ApiError(429, "Too many sign-in attempts. Wait a minute.")
        state["logins"].append(now)
        data = await json_body(request)
        username, password = str(data.get("username", "")), str(data.get("password", ""))
        if not username or not password or len(password) > 256:
            raise ApiError(400, "Enter your username and password.")
        try:
            token, s = await run_in_threadpool(accounts.login, username, password,
                                               str(data.get("totp", "")))
        except AuthError as e:
            status = 429 if e.retry_after else 401
            raise ApiError(status, str(e), needs_totp=e.needs_totp, retry_after=e.retry_after)
        store.add_event("phone", "login", s.username, "signed in to the website")
        resp = JSONResponse(session_payload(s))
        resp.set_cookie(cookie, token, max_age=int(cfg.session_max_days * 86400), path="/",
                        httponly=True, secure=not cfg.web_dev, samesite="strict")
        return resp

    async def whoami(request: Request):
        return JSONResponse(session_payload(session_of(request)))

    async def logout(request: Request):
        accounts.logout(writer(request))
        resp = JSONResponse({"ok": True})
        resp.delete_cookie(cookie, path="/", httponly=True, secure=not cfg.web_dev, samesite="strict")
        return resp

    # ---- door -------------------------------------------------------------
    async def door_status(request: Request):
        session_of(request)
        return JSONResponse(door.status())

    async def door_command(request: Request):
        s = writer(request)
        action = request.path_params["action"]
        h = request.headers
        problem = accounts.verify_command(
            s, request.method, request.url.path, await request.body(),
            h.get("x-cmd-time", ""), h.get("x-cmd-nonce", ""), h.get("x-cmd-signature", ""))
        if problem:
            store.add_event("phone", "rejected", s.username, f"{action} command: {problem}")
            raise ApiError(409, f"Command rejected: {problem}.")
        try:
            if action == "unlock":
                await run_in_threadpool(door.unlock_then_relock)
                store.add_event("phone", "granted", s.username, "remote unlock",
                                clip_reason="remote command")
            else:
                await run_in_threadpool(door.lock)
                store.add_event("phone", "locked", s.username, "remote lock",
                                clip_reason="remote command")
        except Exception as e:                    # servo or GPIO failure: say so, never claim success
            store.add_event("phone", "fault", s.username, f"{action} failed: {e}")
            raise ApiError(502, "The lock did not respond. State not confirmed.")
        return JSONResponse(door.status())

    # ---- people -----------------------------------------------------------
    async def people_list(request: Request):
        session_of(request)
        return JSONResponse([person_json(p) for p in store.people()])

    async def people_add(request: Request):
        s = writer(request)
        form = await multipart(request)
        name = str(form.get("name", "")).strip()
        if not (1 <= len(name) <= 40):
            raise ApiError(400, "Enter a name (up to 40 characters).")
        if form.get("consent") != "true":
            raise ApiError(400, "The person must agree to face enrolment first.")
        role = "guest" if form.get("role") == "guest" else "resident"
        rule = rule_from({k: form.get(k) for k in ("days", "start", "end", "valid_from", "valid_until")
                          if form.get(k) not in (None, "")})
        images = await photos_from(form)
        if not images:
            raise ApiError(400, "Add at least one photo of the person's face.")
        pid = store.add_person(name, role, consent=True, rule=rule)
        try:
            added, problems = await run_in_threadpool(enrol, pid, images)
        except Exception:
            store.remove_person(pid)
            raise
        if not added:
            store.remove_person(pid)
            raise ApiError(400, "No usable photo. " + " ".join(problems))
        store.add_event("phone", "person_added", name, f"{role} added by {s.username}")
        return JSONResponse({"person": person_json(store.person(pid)), "problems": problems}, 201)

    def existing(request: Request) -> Person:
        p = store.person(request.path_params["id"])
        if p is None:
            raise ApiError(404, "That person does not exist.")
        return p

    async def people_photos(request: Request):
        s = writer(request)
        p = existing(request)
        images = await photos_from(await multipart(request))
        if not images:
            raise ApiError(400, "Choose a photo.")
        added, problems = await run_in_threadpool(enrol, p.id, images)
        if not added:
            raise ApiError(400, "No usable photo. " + " ".join(problems))
        store.add_event("phone", "person_changed", p.name, f"{added} photo(s) added by {s.username}")
        return JSONResponse({"person": person_json(store.person(p.id)), "problems": problems})

    async def people_change(request: Request):
        s = writer(request)
        p = existing(request)
        data = await json_body(request)
        changes = []
        if "access" in data:
            if not isinstance(data["access"], dict):
                raise ApiError(400, "Access time is not valid.")
            store.set_access(p.id, rule_from(data["access"]))
            changes.append("access time changed")
        if "enabled" in data:
            store.set_enabled(p.id, bool(data["enabled"]))
            changes.append("enabled" if data["enabled"] else "disabled")
        if changes:
            store.add_event("phone", "person_changed", p.name, f"{', '.join(changes)} by {s.username}")
        return JSONResponse(person_json(store.person(p.id)))

    async def people_remove(request: Request):
        s = writer(request)
        p = existing(request)
        store.remove_person(p.id)
        store.add_event("phone", "person_removed", p.name, f"removed by {s.username}; face data deleted")
        return JSONResponse({"ok": True})

    # ---- log --------------------------------------------------------------
    async def events(request: Request):
        session_of(request)
        try:
            limit = max(1, min(int(request.query_params.get("limit", 100)), cfg.log_keep))
        except ValueError:
            raise ApiError(400, "limit must be a number.")
        return JSONResponse([e.__dict__ for e in store.events(limit)])

    # ---- pages ------------------------------------------------------------
    async def index(request: Request):
        return FileResponse(STATIC / "index.html")

    async def api_error(request: Request, exc: ApiError):
        headers = {"Retry-After": str(exc.extra["retry_after"])} if exc.extra.get("retry_after") else None
        return JSONResponse({"error": exc.message, **exc.extra}, exc.status, headers=headers)

    async def unexpected(request: Request, exc: Exception):
        return JSONResponse({"error": "Something went wrong on the door unit."}, 500)

    routes = [
        Route("/", index),
        Route("/api/login", login, methods=["POST"]),
        Route("/api/session", whoami),
        Route("/api/logout", logout, methods=["POST"]),
        Route("/api/door", door_status),
        Route("/api/door/{action:str}", door_command, methods=["POST"]),
        Route("/api/people", people_list),
        Route("/api/people", people_add, methods=["POST"]),
        Route("/api/people/{id:int}", people_change, methods=["PATCH"]),
        Route("/api/people/{id:int}", people_remove, methods=["DELETE"]),
        Route("/api/people/{id:int}/photos", people_photos, methods=["POST"]),
        Route("/api/events", events),
        Mount("/static", StaticFiles(directory=STATIC)),
    ]
    app = Starlette(routes=routes, exception_handlers={ApiError: api_error, Exception: unexpected})

    async def with_security_headers(scope, receive, send):
        if scope["type"] != "http":
            return await app(scope, receive, send)

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                headers = [(k, v) for k, v in message.get("headers", []) if k.lower() != b"server"]
                extra = {
                    "Content-Security-Policy": CSP,
                    "X-Content-Type-Options": "nosniff",
                    "X-Frame-Options": "DENY",
                    "Referrer-Policy": "no-referrer",
                    "Permissions-Policy": "geolocation=(), microphone=(), camera=()",
                    "Cross-Origin-Opener-Policy": "same-origin",
                    "Cross-Origin-Resource-Policy": "same-origin",
                    "Cache-Control": "no-store" if scope["path"].startswith("/api") else "no-cache",
                }
                if not cfg.web_dev:
                    extra["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
                headers += [(k.lower().encode(), v.encode()) for k, v in extra.items()]
                message = {**message, "headers": headers}
            await send(message)
        await app(scope, receive, send_wrapper)

    with_security_headers.inner = app
    return with_security_headers
