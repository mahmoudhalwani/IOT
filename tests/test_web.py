import hashlib
import hmac
import secrets
import time
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from doorbell.door import Door
from doorbell.hardware import SimLock
from doorbell.web.accounts import Accounts, totp_code
from doorbell.web.app import create_app

ORIGIN = "https://testserver"
PASSWORD = "correct horse battery"
LIVE = Path(__file__).parent / "fixtures" / "live_1.jpg"


class Clock:
    def __init__(self):
        self.offset = 0.0

    def __call__(self):
        return time.time() + self.offset


@pytest.fixture
def web(cfg, store, engine):
    cfg.relock_seconds = 0.3
    clock = Clock()
    accounts = Accounts(store, cfg, clock)
    accounts.add_user("alaa", PASSWORD)
    said = []
    door = Door(SimLock(said.append), cfg.relock_seconds)
    door.lock()
    app = create_app(cfg, store, accounts, door, lambda: engine, clock)
    client = TestClient(app, base_url=ORIGIN)

    class Web:
        pass
    w = Web()
    w.client, w.accounts, w.door, w.store, w.clock, w.cfg = client, accounts, door, store, clock, cfg
    yield w
    door.close()


def login(w, **extra):
    r = w.client.post("/api/login", json={"username": "alaa", "password": PASSWORD, **extra})
    assert r.status_code == 200, r.text
    return r.json()


def headers(s):
    return {"Origin": ORIGIN, "X-CSRF-Token": s["csrf"]}


def signed(s, path, *, stamp=None, nonce=None, key=None):
    stamp = stamp or str(int(time.time()))
    nonce = nonce or secrets.token_hex(16)
    msg = "\n".join(["POST", path, stamp, nonce, ""]).encode()
    sig = hmac.new(key or bytes.fromhex(s["cmd_key"]), msg, hashlib.sha256).hexdigest()
    return {**headers(s), "X-Cmd-Time": stamp, "X-Cmd-Nonce": nonce, "X-Cmd-Signature": sig}


# ---- sign-in ---------------------------------------------------------------
def test_everything_needs_a_login(web):
    for path in ("/api/door", "/api/people", "/api/events", "/api/session"):
        assert web.client.get(path).status_code == 401
    assert web.client.post("/api/door/unlock", headers={"Origin": ORIGIN}).status_code == 401
    assert web.client.delete("/api/people/1", headers={"Origin": ORIGIN}).status_code == 401
    assert web.door.locked is True


def test_cookie_is_locked_down_and_pages_have_security_headers(web):
    r = web.client.post("/api/login", json={"username": "alaa", "password": PASSWORD})
    cookie = r.headers["set-cookie"].lower()
    assert cookie.startswith("__host-session=")
    for flag in ("httponly", "secure", "samesite=strict", "path=/"):
        assert flag in cookie
    assert "domain=" not in cookie
    page = web.client.get("/")
    assert "default-src 'self'" in page.headers["content-security-policy"]
    assert page.headers["x-frame-options"] == "DENY"
    assert "max-age" in page.headers["strict-transport-security"]
    assert r.headers["cache-control"] == "no-store"
    assert "<script>" not in page.text and "onclick" not in page.text      # nothing inline
    assert web.client.get("/docs").status_code == 404
    assert web.client.get("/static/../accounts.py").status_code == 404


def test_wrong_logins_look_the_same_and_pause_the_account(web):
    a = web.client.post("/api/login", json={"username": "alaa", "password": "wrong password 1"})
    b = web.client.post("/api/login", json={"username": "nobody", "password": "wrong password 1"})
    assert a.status_code == b.status_code == 401 and a.json() == b.json()
    for _ in range(web.cfg.login_fail_limit):
        web.client.post("/api/login", json={"username": "alaa", "password": "wrong password 1"})
    r = web.client.post("/api/login", json={"username": "alaa", "password": PASSWORD})
    assert r.status_code == 429 and int(r.headers["retry-after"]) > 0     # even the right password waits
    web.clock.offset += 16 * 60
    assert web.client.post("/api/login", json={"username": "alaa", "password": PASSWORD}).status_code == 200


def test_session_expires_and_logout_ends_it(web):
    s = login(web)
    assert web.client.get("/api/door").status_code == 200
    assert web.client.post("/api/logout", headers=headers(s)).status_code == 200
    assert web.client.get("/api/door").status_code == 401
    login(web)
    web.clock.offset += web.cfg.session_idle_hours * 3600 + 60
    assert web.client.get("/api/door").status_code == 401


def test_session_token_is_not_stored_in_the_database(web, cfg):
    login(web)
    token = web.client.cookies.get("__Host-session")
    raw = b"".join(p.read_bytes() for p in cfg.data_dir.glob("doorbell.db*"))
    assert token.encode() not in raw and PASSWORD.encode() not in raw


def test_two_step_code(web):
    secret_text, _ = web.accounts.enable_totp("alaa")
    import base64
    secret = base64.b32decode(secret_text)
    r = web.client.post("/api/login", json={"username": "alaa", "password": PASSWORD})
    assert r.status_code == 401 and r.json()["needs_totp"] is True
    r = web.client.post("/api/login", json={"username": "alaa", "password": PASSWORD, "totp": "000000"})
    assert r.status_code == 401
    code = totp_code(secret, int(time.time() // 30))
    login(web, totp=code)
    r = web.client.post("/api/login", json={"username": "alaa", "password": PASSWORD, "totp": code})
    assert r.status_code == 401                                            # a code works once


def test_totp_matches_the_published_test_vector():
    assert totp_code(b"12345678901234567890", 59 // 30) == "287082"       # RFC 6238, T = 59 s


def test_password_rules(web):
    with pytest.raises(ValueError):
        web.accounts.add_user("sara", "short")
    with pytest.raises(ValueError):
        web.accounts.add_user("alaa", PASSWORD)


# ---- door commands ---------------------------------------------------------
def test_remote_unlock_then_automatic_relock_and_log(web):
    s = login(web)
    r = web.client.post("/api/door/unlock", headers=signed(s, "/api/door/unlock"))
    assert r.status_code == 200 and r.json()["state"] == "unlocked"
    assert web.door.locked is False
    time.sleep(web.cfg.relock_seconds + 0.3)
    assert web.door.locked is True and web.client.get("/api/door").json()["state"] == "locked"
    e = web.store.events()[0]
    assert (e.method, e.result, e.person) == ("phone", "granted", "alaa")


def test_lock_command(web):
    s = login(web)
    web.client.post("/api/door/unlock", headers=signed(s, "/api/door/unlock"))
    r = web.client.post("/api/door/lock", headers=signed(s, "/api/door/lock"))
    assert r.json()["state"] == "locked" and web.door.locked is True


def test_replayed_changed_old_and_unsigned_commands_are_rejected(web):
    s = login(web)
    good = signed(s, "/api/door/lock")
    assert web.client.post("/api/door/lock", headers=good).status_code == 200
    attempts = {
        "replayed": ("/api/door/lock", good),
        "changed (lock signature on unlock)": ("/api/door/unlock", signed(s, "/api/door/lock")),
        "changed signature": ("/api/door/unlock", {**signed(s, "/api/door/unlock"), "X-Cmd-Signature": "00" * 32}),
        "old": ("/api/door/unlock", signed(s, "/api/door/unlock", stamp=str(int(time.time()) - 120))),
        "wrong key": ("/api/door/unlock", signed(s, "/api/door/unlock", key=b"x" * 32)),
        "unsigned": ("/api/door/unlock", headers(s)),
    }
    for name, (path, h) in attempts.items():
        r = web.client.post(path, headers=h)
        assert r.status_code == 409, name
    assert web.door.locked is True
    assert [e.result for e in web.store.events(6)] == ["rejected"] * 6


def test_cross_site_requests_are_refused(web):
    s = login(web)
    sig = signed(s, "/api/door/unlock")
    for h in ({**sig, "Origin": "https://evil.example"},
              {k: v for k, v in sig.items() if k != "Origin"},
              {k: v for k, v in sig.items() if k != "X-CSRF-Token"},
              {**sig, "X-CSRF-Token": "guess"}):
        assert web.client.post("/api/door/unlock", headers=h).status_code == 403
    assert web.door.locked is True


def test_another_sessions_key_cannot_command(web):
    first = login(web)
    second = login(web)                                   # cookie now belongs to the second session
    r = web.client.post("/api/door/unlock",
                        headers={**signed(first, "/api/door/unlock"), "X-CSRF-Token": second["csrf"]})
    assert r.status_code == 409 and web.door.locked is True


# ---- people ----------------------------------------------------------------
def add_person(w, s, **fields):
    data = {"name": "Sara", "role": "resident", "consent": "true", **fields}
    with open(LIVE, "rb") as fh:
        return w.client.post("/api/people", headers=headers(s), data=data,
                             files=[("photos", ("sara.jpg", fh, "image/jpeg"))])


def test_add_list_change_and_remove_a_person(web):
    s = login(web)
    r = add_person(web, s, role="guest", days="0,3", start="09:00", end="12:00",
                   valid_until="2030-01-01T00:00")
    assert r.status_code == 201, r.text
    p = r.json()["person"]
    assert p["photos"] == 1 and p["role"] == "guest"
    assert p["access"] == {"days": [0, 3], "start": "09:00", "end": "12:00",
                           "valid_from": None, "valid_until": "2030-01-01T00:00"}
    assert len(web.store.templates()) == 1

    r = web.client.patch(f"/api/people/{p['id']}", headers=headers(s),
                         json={"access": {"days": None, "start": None, "end": None}, "enabled": False})
    assert r.json()["access"]["days"] is None and r.json()["enabled"] is False
    assert web.store.templates() == []                    # disabled people are not matched

    assert web.client.delete(f"/api/people/{p['id']}", headers=headers(s)).status_code == 200
    assert web.client.get("/api/people").json() == []
    assert web.client.delete(f"/api/people/{p['id']}", headers=headers(s)).status_code == 404
    results = [e.result for e in web.store.events()]
    assert "person_added" in results and "person_removed" in results


def test_enrolment_is_refused_without_consent_photo_or_valid_times(web):
    s = login(web)
    assert add_person(web, s, consent="false").status_code == 400
    assert add_person(web, s, start="09:00").status_code == 400            # start without end
    assert add_person(web, s, days="9").status_code == 400
    r = web.client.post("/api/people", headers=headers(s), data={"name": "Sara", "consent": "true"})
    assert r.status_code == 400
    r = web.client.post("/api/people", headers=headers(s), data={"name": "Sara", "consent": "true"},
                        files=[("photos", ("x.jpg", b"this is not an image", "image/jpeg"))])
    assert r.status_code == 400
    assert web.store.people() == []                       # nothing half-created


def test_same_face_cannot_be_enrolled_as_two_people(web):
    s = login(web)
    assert add_person(web, s).status_code == 201
    r = add_person(web, s, name="Intruder")
    assert r.status_code == 400 and "already belongs to Sara" in r.json()["error"]
    assert [p.name for p in web.store.people()] == ["Sara"]


# ---- log -------------------------------------------------------------------
def test_log_can_be_read_but_not_edited_or_deleted(web):
    s = login(web)
    events = web.client.get("/api/events").json()
    assert events and events[0]["result"] == "login"
    for method in ("post", "put", "patch", "delete"):
        r = getattr(web.client, method)("/api/events", headers=headers(s))
        assert r.status_code == 405
    assert web.client.delete(f"/api/events/{events[0]['id']}", headers=headers(s)).status_code == 404
    assert len(web.client.get("/api/events").json()) == len(events)
