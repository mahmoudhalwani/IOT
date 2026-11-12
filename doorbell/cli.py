"""Command-line tool for the door unit.

    python -m doorbell add "Alaa" --consent photo1.jpg photo2.jpg
    python -m doorbell add "Cleaner" --guest --consent --days mon,thu --hours 09:00-12:00 \\
                         --until 2026-12-31T23:59 photo.jpg
    python -m doorbell photo 1 --camera                    (one more photo for person 1)
    python -m doorbell list
    python -m doorbell access 2 --hours 08:00-18:00        (use --always to clear limits)
    python -m doorbell remove 2
    python -m doorbell check photo.jpg                     (what would the door decide?)
    python -m doorbell watch                               (live camera test)
    python -m doorbell user add alaa                       (website account)
    python -m doorbell run                                 (the real thing: door + website)
    python -m doorbell log                                 (access log)
    python -m doorbell servo 90                            (move the servo by hand, Pi only)
"""
from __future__ import annotations

import argparse
import getpass
import sys
import threading
import time
from datetime import datetime

import cv2

from .access import DAY_NAMES, AccessRule, parse_days, parse_hours
from .config import Config
from .crypto import Vault, load_or_create_key
from .decision import Outcome, Visit
from .door import Door
from .enroll import EnrollError, add_face
from .face import FaceEngine, Gallery
from .media import MediaStore
from . import hardware
from .controller import Controller
from .store import Store


def open_store(cfg: Config) -> Store:
    return Store(cfg.db_path, Vault(load_or_create_key(cfg.key_path)), cfg.log_keep)


def open_camera(cfg: Config) -> cv2.VideoCapture:
    cam = cv2.VideoCapture(cfg.camera_index)
    cam.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
    cam.set(cv2.CAP_PROP_FRAME_WIDTH, cfg.frame_width)
    cam.set(cv2.CAP_PROP_FRAME_HEIGHT, cfg.frame_height)
    cam.set(cv2.CAP_PROP_BUFFERSIZE, 1)
    if not cam.isOpened():
        sys.exit(f"cannot open camera {cfg.camera_index}")
    for _ in range(20):                 # webcams give dark frames until exposure settles
        cam.read()
    return cam


def load_photos(paths) -> list:
    images = []
    for path in paths:
        img = cv2.imread(path)
        if img is None:
            sys.exit(f"cannot read image {path}")
        images.append((path, img))
    return images


def camera_shots(cfg: Config, engine: FaceEngine, count: int = 3) -> list:
    """Takes `count` photos. Each one waits (up to 8 s) until a face is in view."""
    cam, shots = open_camera(cfg), []
    for i in range(count):
        input(f"photo {i + 1}/{count}: look at the camera and press Enter ")
        deadline, frame, found = time.monotonic() + 8, None, False
        while time.monotonic() < deadline and not found:
            for _ in range(4):          # drop stale buffered frames
                cam.read()
            ok, frame = cam.read()
            found = ok and bool(engine.detect(frame))
        if found:
            shots.append((f"camera shot {i + 1}", frame))
            print("  captured")
        else:
            print("  no face seen for 8 s; check the light and that the camera is not covered")
    cam.release()
    return shots


def enrol_images(engine, gallery, store, cfg, pid: int, images: list) -> int:
    added = 0
    for label, img in images:
        try:
            r = add_face(engine, gallery, store, cfg, pid, img)
            print(f"  ok   {label} (face {r.face_width}px wide)")
            added += 1
        except EnrollError as e:
            print(f"  skip {label}: {e}")
    return added


def rule_from_args(a: argparse.Namespace) -> AccessRule:
    start, end = parse_hours(a.hours) if a.hours else (None, None)
    return AccessRule(
        valid_from=datetime.fromisoformat(a.start) if a.start else None,
        valid_until=datetime.fromisoformat(a.until) if a.until else None,
        days=parse_days(a.days) if a.days else None, start=start, end=end)


def describe(rule: AccessRule) -> str:
    parts = []
    if rule.days is not None:
        parts.append(",".join(DAY_NAMES[d] for d in sorted(rule.days)))
    if rule.start and rule.end:
        parts.append(f"{rule.start:%H:%M}-{rule.end:%H:%M}")
    if rule.valid_from:
        parts.append(f"from {rule.valid_from:%Y-%m-%d %H:%M}")
    if rule.valid_until:
        parts.append(f"until {rule.valid_until:%Y-%m-%d %H:%M}")
    return " ".join(parts) or "always"


def add_rule_options(p: argparse.ArgumentParser) -> None:
    p.add_argument("--days", help="allowed weekdays, e.g. mon,tue,fri")
    p.add_argument("--hours", help="allowed daily window, e.g. 08:00-18:00")
    p.add_argument("--start", help="access begins, e.g. 2026-12-01T09:00")
    p.add_argument("--until", help="access expires, e.g. 2026-12-31T23:59")


def cmd_add(a, cfg):
    if not a.consent:
        sys.exit("refusing: pass --consent to confirm this person agreed to face enrolment")
    if not a.photos and not a.camera:
        sys.exit("give one or more photo files, or --camera")
    store, engine, gallery = open_store(cfg), FaceEngine(cfg), Gallery(cfg)
    gallery.load(store.templates())
    images = load_photos(a.photos)
    if a.camera:
        images += camera_shots(cfg, engine)
    if not images:
        sys.exit("no photo was taken; nobody was added")
    pid = store.add_person(a.name, "guest" if a.guest else "resident",
                           consent=True, rule=rule_from_args(a))
    added = enrol_images(engine, gallery, store, cfg, pid, images)
    if not added:
        store.remove_person(pid)
        sys.exit("no usable photo; nobody was added "
                 "(to add photos to someone already enrolled use: python -m doorbell photo <id>)")
    print(f"added {a.name} as id {pid} with {added} photo(s)")


def cmd_photo(a, cfg):
    if not a.photos and not a.camera:
        sys.exit("give one or more photo files, or --camera")
    store, engine, gallery = open_store(cfg), FaceEngine(cfg), Gallery(cfg)
    gallery.load(store.templates())
    person = store.person(a.id)
    if person is None:
        sys.exit(f"no person with id {a.id}")
    images = load_photos(a.photos)
    if a.camera:
        images += camera_shots(cfg, engine, 1)
    added = enrol_images(engine, gallery, store, cfg, a.id, images)
    print(f"{person.name} now has {store.person(a.id).template_count} photo(s) ({added} added)")


def cmd_list(a, cfg):
    people = open_store(cfg).people()
    if not people:
        print("nobody enrolled yet")
    for p in people:
        state = "" if p.enabled else "  [disabled]"
        print(f"{p.id:>3}  {p.name:<20} {p.role:<9} photos={p.template_count}  "
              f"access: {describe(p.rule)}{state}")


def cmd_access(a, cfg):
    store = open_store(cfg)
    store.set_access(a.id, AccessRule() if a.always else rule_from_args(a))
    print(f"access for id {a.id}: {describe(store.person(a.id).rule)}")


def cmd_remove(a, cfg):
    store = open_store(cfg)
    person = store.person(a.id)
    if person is None:
        sys.exit(f"no person with id {a.id}")
    t0 = time.perf_counter()
    store.remove_person(a.id)
    print(f"removed {person.name} and their face data in {time.perf_counter() - t0:.3f} s")


def cmd_enable(a, cfg):
    open_store(cfg).set_enabled(a.id, a.command == "enable")
    print(f"id {a.id} {a.command}d")


def cmd_check(a, cfg):
    store, engine, gallery = open_store(cfg), FaceEngine(cfg), Gallery(cfg)
    gallery.load(store.templates())
    img = cv2.imread(a.photo)
    if img is None:
        sys.exit(f"cannot read image {a.photo}")
    faces = engine.detect(img)
    if not faces:
        sys.exit("no face found")
    face = faces[0]
    live = engine.liveness(img, face)
    pid, sim = gallery.match(engine.embed(img, face))
    who = store.person(pid).name if pid else "unknown"
    print(f"face {face.w}px wide, well placed: {engine.well_placed(img, face)}")
    print(f"liveness: p_real={live.p_real:.3f} -> {'LIVE' if live.passed(cfg.live_threshold) else 'SPOOF'}")
    print(f"identity: {who} (similarity {sim:.3f}, threshold {cfg.match_threshold})")


def cmd_watch(a, cfg):
    store, engine, gallery = open_store(cfg), FaceEngine(cfg), Gallery(cfg)
    gallery.load(store.templates())
    cam = open_camera(cfg)
    print("watching the camera; Ctrl+C to stop")

    def frame():
        ok, img = cam.read()
        if not ok:
            sys.exit("camera read failed")
        return img

    try:
        while True:
            visit, started, hint = Visit(engine, gallery, store, cfg), None, None
            while True:                                  # one visit, until a final decision
                img = frame()
                t0 = time.perf_counter()
                d = visit.update(img)
                ms = (time.perf_counter() - t0) * 1000
                if d.outcome is Outcome.NO_FACE:
                    continue
                started = started or time.perf_counter() - ms / 1000
                if a.verbose:
                    print(f"  {d.outcome.value:<10} sim={d.similarity:.2f} live={d.p_real:.2f} "
                          f"{ms:.0f} ms {d.detail}")
                elif d.outcome is Outcome.POSITION and d.detail != hint:
                    hint = d.detail
                    print(f"  ({hint})")
                if d.final:
                    break
            took = time.perf_counter() - started
            stamp = f"{datetime.now():%H:%M:%S}"
            if d.outcome is Outcome.GRANTED:
                print(f"{stamp} GRANTED  {d.name}  (decided in {took:.1f} s, similarity "
                      f"{d.similarity:.2f}) -> door UNLOCKED")
                time.sleep(cfg.relock_seconds)
                print(f"{datetime.now():%H:%M:%S} door LOCKED again")
            else:
                print(f"{stamp} DENIED   {d.outcome.value}  {d.name or ''} {d.detail} "
                      f"(decided in {took:.1f} s) -> door stays locked")
            # Like the real door: do not judge the same person again while they are
            # still standing there. Re-arm when they leave, or after 10 s anyway.
            print("         (step out of view to test again; Ctrl+C to stop)")
            clear, deadline = 0, time.monotonic() + 10
            while clear < 15 and time.monotonic() < deadline:
                clear = clear + 1 if not engine.detect(frame()) else 0
            print("ready for the next visitor")
    except KeyboardInterrupt:
        pass
    finally:
        cam.release()


def cmd_run(a, cfg):
    def say(msg):
        print(f"{datetime.now():%H:%M:%S} {msg}", flush=True)

    store, engine, gallery = open_store(cfg), FaceEngine(cfg), Gallery(cfg)
    lock, motion = hardware.build(cfg, say)
    door = Door(lock, cfg.relock_seconds)
    media = MediaStore(cfg, store.vault)
    controller = Controller(cfg, engine, gallery, store, door, hardware.Camera(cfg), motion,
                            media=media, say=say)
    say("starting; Ctrl+C to stop")
    try:
        if a.no_web:
            import signal
            signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))   # so "stop" still locks the door
            controller.run()
            return
        import uvicorn
        from .web.accounts import Accounts
        from .web.app import create_app

        accounts = Accounts(store, cfg)
        if not accounts.users():
            say('no website user yet: create one with  python -m doorbell user add <name>')
        app = create_app(cfg, store, accounts, door, lambda: FaceEngine(cfg), media=media)
        server = uvicorn.Server(uvicorn.Config(
            app, host=cfg.web_host, port=cfg.web_port, log_level="warning",
            server_header=False, proxy_headers=False))

        def door_loop():
            try:
                controller.run()
            except Exception as e:               # door loop died: stop everything so the
                say(f"FAULT door loop stopped: {e}")   # service manager restarts it cleanly
            finally:
                server.should_exit = True

        worker = threading.Thread(target=door_loop, name="door", daemon=True)
        worker.start()
        say(f"website on http://{cfg.web_host}:{cfg.web_port}")
        server.run()                             # returns on Ctrl+C or a stop signal
        controller.stop.set()
        worker.join(timeout=cfg.relock_seconds + 5)
    except KeyboardInterrupt:
        pass
    finally:
        door.lock()                              # whatever happened, end locked
        door.close()
        say("stopped; door locked")


def cmd_user(a, cfg):
    from .web.accounts import Accounts
    accounts = Accounts(open_store(cfg), cfg)

    def new_password():
        first = getpass.getpass("new password (at least 10 characters): ")
        if first != getpass.getpass("repeat password: "):
            sys.exit("passwords do not match")
        return first

    if a.action == "list":
        for name, totp in accounts.users():
            print(f"{name:<20} {'2-step code: on' if totp else '2-step code: off'}")
        if not accounts.users():
            print("no website users yet")
        return
    if not a.name:
        sys.exit("give the username")
    if a.action == "add":
        accounts.add_user(a.name, new_password())
        print(f"user {a.name} created")
    elif a.action == "password":
        accounts.set_password(a.name, new_password())
        print("password changed; that user is signed out everywhere")
    elif a.action == "remove":
        accounts.remove_user(a.name)
        print(f"user {a.name} removed")
    elif a.action == "totp":
        secret, link = accounts.enable_totp(a.name)
        print("In your authenticator app choose 'enter a setup key' and type:")
        print(f"  account: Doorbell ({a.name})\n  key:     {secret}\n  type:    time-based")
        print(f"or open this link on the phone:\n  {link}")
    elif a.action == "totp-off":
        accounts.disable_totp(a.name)
        print("2-step code turned off")


def cmd_log(a, cfg):
    events = open_store(cfg).events(a.count)
    if not events:
        print("the log is empty")
    for e in reversed(events):
        print(f"{e.at.replace('T', ' ')}  {e.method:<6} {e.result:<19} {e.person:<16} {e.detail}")


def cmd_servo(a, cfg):
    cfg.hardware = "pi"
    lock = hardware.ServoLock(cfg)
    if a.position == "lock":
        lock.lock()
    elif a.position == "unlock":
        lock.unlock()
    else:
        lock.move_to(float(a.position))
    print(f"servo moved to {a.position}")
    lock.close()


def main() -> None:
    ap = argparse.ArgumentParser(prog="python -m doorbell", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="command", required=True)

    p = sub.add_parser("add", help="enrol a person from photos or the camera")
    p.add_argument("name")
    p.add_argument("photos", nargs="*")
    p.add_argument("--camera", action="store_true", help="take 3 photos with the door camera")
    p.add_argument("--guest", action="store_true")
    p.add_argument("--consent", action="store_true", help="the person agreed to face enrolment")
    add_rule_options(p)
    p.set_defaults(fn=cmd_add)

    p = sub.add_parser("photo", help="add more photos to someone already enrolled")
    p.add_argument("id", type=int)
    p.add_argument("photos", nargs="*")
    p.add_argument("--camera", action="store_true", help="take one photo with the door camera")
    p.set_defaults(fn=cmd_photo)

    sub.add_parser("list", help="show enrolled people").set_defaults(fn=cmd_list)

    p = sub.add_parser("access", help="change when a person may enter")
    p.add_argument("id", type=int)
    p.add_argument("--always", action="store_true", help="remove all time limits")
    add_rule_options(p)
    p.set_defaults(fn=cmd_access)

    p = sub.add_parser("remove", help="delete a person and their face data")
    p.add_argument("id", type=int)
    p.set_defaults(fn=cmd_remove)

    for name in ("enable", "disable"):
        p = sub.add_parser(name, help=f"{name} a person without deleting them")
        p.add_argument("id", type=int)
        p.set_defaults(fn=cmd_enable)

    p = sub.add_parser("check", help="run the pipeline on one photo")
    p.add_argument("photo")
    p.set_defaults(fn=cmd_check)

    p = sub.add_parser("watch", help="live decisions from the camera")
    p.add_argument("-v", "--verbose", action="store_true", help="print every frame's scores")
    p.set_defaults(fn=cmd_watch)

    p = sub.add_parser("run", help="run the door (sensor, camera, lock) and the website")
    p.add_argument("--no-web", action="store_true", help="door only, no website")
    p.set_defaults(fn=cmd_run)

    p = sub.add_parser("user", help="website accounts")
    p.add_argument("action", choices=["add", "list", "password", "remove", "totp", "totp-off"])
    p.add_argument("name", nargs="?")
    p.set_defaults(fn=cmd_user)

    p = sub.add_parser("log", help="show the access log")
    p.add_argument("count", type=int, nargs="?", default=30)
    p.set_defaults(fn=cmd_log)

    p = sub.add_parser("servo", help="move the servo by hand (Pi only), to find the right angles")
    p.add_argument("position", help="lock, unlock, or an angle from 0 to 180")
    p.set_defaults(fn=cmd_servo)

    args = ap.parse_args()
    try:
        args.fn(args, Config.load())
    except (KeyError, ValueError, FileNotFoundError) as e:
        sys.exit(f"error: {e}")
