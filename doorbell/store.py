"""SQLite storage for people and their face templates.

Face templates are 128-number vectors, not photos; they are stored encrypted and
enrolment photos are never written to disk. Removing a person deletes their
templates in the same transaction, and `secure_delete` overwrites the freed
pages so the data cannot be recovered from the file (MH-11).
"""
from __future__ import annotations

import functools
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, time
from pathlib import Path
from typing import Optional

import numpy as np

from .access import AccessRule
from .crypto import Vault

SCHEMA = """
CREATE TABLE IF NOT EXISTS people (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    role        TEXT NOT NULL CHECK (role IN ('resident', 'guest')),
    enabled     INTEGER NOT NULL DEFAULT 1,
    consent_at  TEXT NOT NULL,
    valid_from  TEXT,
    valid_until TEXT,
    days        TEXT,
    start_time  TEXT,
    end_time    TEXT,
    created_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS templates (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    person_id  INTEGER NOT NULL REFERENCES people(id) ON DELETE CASCADE,
    blob       BLOB NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS templates_person ON templates(person_id);
CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    at          TEXT NOT NULL,
    method      TEXT NOT NULL,      -- face, phone, system
    result      TEXT NOT NULL,      -- granted, denied_unknown, denied_spoof, denied_not_allowed, fault, ...
    person      TEXT NOT NULL,      -- name at the time of the event, or "unknown"
    detail      TEXT NOT NULL DEFAULT '',
    clip        TEXT,               -- video reference, if one was recorded
    clip_reason TEXT                -- why there is no clip
);
"""


@dataclass(frozen=True)
class Event:
    id: int
    at: str
    method: str
    result: str
    person: str
    detail: str
    clip: Optional[str]
    clip_reason: Optional[str]


@dataclass(frozen=True)
class Person:
    id: int
    name: str
    role: str
    enabled: bool
    rule: AccessRule
    template_count: int = 0


def _locked(fn):
    """The website and the door loop share one database connection from different
    threads; every public method takes this lock."""
    @functools.wraps(fn)
    def wrapper(self, *a, **kw):
        with self.mutex:
            return fn(self, *a, **kw)
    return wrapper


def _iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.isoformat(timespec="seconds") if dt else None


class Store:
    def __init__(self, path: Path, vault: Vault, log_keep: int = 500):
        self._log_keep = log_keep
        self.mutex = threading.RLock()
        self.vault = vault
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA foreign_keys = ON")
        self._db.execute("PRAGMA secure_delete = ON")
        self._db.execute("PRAGMA journal_mode = WAL")
        self._db.executescript(SCHEMA)
        self.db = self._db
        self._vault = vault

    # ---- people -----------------------------------------------------------
    @_locked
    def add_person(self, name: str, role: str = "resident", *, consent: bool,
                   rule: AccessRule = AccessRule()) -> int:
        if not consent:
            raise ValueError("face data may only be stored after the person agrees (MH-11)")
        name = name.strip()
        if not name:
            raise ValueError("name is required")
        now = _iso(datetime.now())
        with self._db:
            cur = self._db.execute(
                "INSERT INTO people (name, role, consent_at, created_at) VALUES (?, ?, ?, ?)",
                (name, role, now, now))
            self._write_rule(cur.lastrowid, rule)
        return cur.lastrowid

    @_locked
    def set_access(self, person_id: int, rule: AccessRule) -> None:
        with self._db:
            self._require(person_id)
            self._write_rule(person_id, rule)

    @_locked
    def set_enabled(self, person_id: int, enabled: bool) -> None:
        with self._db:
            self._require(person_id)
            self._db.execute("UPDATE people SET enabled = ? WHERE id = ?", (int(enabled), person_id))

    @_locked
    def remove_person(self, person_id: int) -> None:
        """Deletes the person and every face template they have."""
        with self._db:
            self._require(person_id)
            self._db.execute("DELETE FROM people WHERE id = ?", (person_id,))
        self._db.execute("PRAGMA wal_checkpoint(TRUNCATE)")   # purge the copy left in the WAL

    @_locked
    def people(self) -> list[Person]:
        rows = self._db.execute(
            "SELECT p.*, (SELECT COUNT(*) FROM templates t WHERE t.person_id = p.id) AS n "
            "FROM people p ORDER BY p.id").fetchall()
        return [self._person(r) for r in rows]

    @_locked
    def person(self, person_id: int) -> Optional[Person]:
        return next((p for p in self.people() if p.id == person_id), None)

    # ---- face templates ---------------------------------------------------
    @_locked
    def add_template(self, person_id: int, embedding: np.ndarray) -> int:
        vec = np.asarray(embedding, dtype=np.float32).ravel()
        with self._db:
            self._require(person_id)
            cur = self._db.execute(
                "INSERT INTO templates (person_id, blob, created_at) VALUES (?, x'', ?)",
                (person_id, _iso(datetime.now())))
            blob = self._vault.seal(vec.tobytes(), self._ctx(person_id, cur.lastrowid))
            self._db.execute("UPDATE templates SET blob = ? WHERE id = ?", (blob, cur.lastrowid))
        return cur.lastrowid

    @_locked
    def templates(self) -> list[tuple[int, np.ndarray]]:
        """(person_id, embedding) for every enabled person."""
        rows = self._db.execute(
            "SELECT t.id, t.person_id, t.blob FROM templates t "
            "JOIN people p ON p.id = t.person_id WHERE p.enabled = 1").fetchall()
        out = []
        for r in rows:
            raw = self._vault.open(r["blob"], self._ctx(r["person_id"], r["id"]))
            out.append((r["person_id"], np.frombuffer(raw, dtype=np.float32)))
        return out

    # ---- access log (MH-06): entries can be added and read, never edited ---
    @_locked
    def add_event(self, method: str, result: str, person: str = "unknown", detail: str = "",
                  clip: Optional[str] = None, clip_reason: Optional[str] = None) -> int:
        with self._db:
            cur = self._db.execute(
                "INSERT INTO events (at, method, result, person, detail, clip, clip_reason) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (_iso(datetime.now()), method, result, person, detail, clip, clip_reason))
            self._db.execute("DELETE FROM events WHERE id <= ?", (cur.lastrowid - self._log_keep,))
        return cur.lastrowid

    @_locked
    def events(self, limit: int = 50) -> list[Event]:
        """Newest first."""
        rows = self._db.execute("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [Event(*r) for r in rows]

    # ---- internals --------------------------------------------------------
    @staticmethod
    def _ctx(person_id: int, template_id: int) -> bytes:
        return f"template:{person_id}:{template_id}".encode()

    def _require(self, person_id: int) -> None:
        if not self._db.execute("SELECT 1 FROM people WHERE id = ?", (person_id,)).fetchone():
            raise KeyError(f"no person with id {person_id}")

    def _write_rule(self, person_id: int, rule: AccessRule) -> None:
        days = ",".join(str(d) for d in sorted(rule.days)) if rule.days is not None else None
        self._db.execute(
            "UPDATE people SET valid_from = ?, valid_until = ?, days = ?, start_time = ?, "
            "end_time = ? WHERE id = ?",
            (_iso(rule.valid_from), _iso(rule.valid_until), days,
             rule.start.isoformat("minutes") if rule.start else None,
             rule.end.isoformat("minutes") if rule.end else None, person_id))

    @staticmethod
    def _person(r: sqlite3.Row) -> Person:
        rule = AccessRule(
            valid_from=datetime.fromisoformat(r["valid_from"]) if r["valid_from"] else None,
            valid_until=datetime.fromisoformat(r["valid_until"]) if r["valid_until"] else None,
            days=frozenset(int(d) for d in r["days"].split(",")) if r["days"] else None,
            start=time.fromisoformat(r["start_time"]) if r["start_time"] else None,
            end=time.fromisoformat(r["end_time"]) if r["end_time"] else None)
        return Person(r["id"], r["name"], r["role"], bool(r["enabled"]), rule, r["n"])
