"""Website accounts, sessions and login protection.

- Passwords are stored as Argon2id hashes.
- A session is a random 256-bit token; only its SHA-256 is stored, so a copy of
  the database cannot be used to sign in.
- Each session also has a CSRF token and a command-signing key (stored encrypted).
- Optional second factor: standard TOTP codes (Google Authenticator, Aegis, ...).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import struct
import time
from dataclasses import dataclass
from typing import Optional
from urllib.parse import quote

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

from ..config import Config
from ..store import Store

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT NOT NULL UNIQUE COLLATE NOCASE,
    password_hash TEXT NOT NULL,
    totp_secret   BLOB,
    totp_last     INTEGER NOT NULL DEFAULT 0,
    created_at    INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    csrf       TEXT NOT NULL,
    cmd_key    BLOB NOT NULL,
    created_at INTEGER NOT NULL,
    seen_at    INTEGER NOT NULL
);
"""
MIN_PASSWORD = 10
_hasher = PasswordHasher()                       # Argon2id, library defaults (RFC 9106 low-memory)
_DUMMY_HASH = _hasher.hash("not a real password")


class AuthError(Exception):
    """Login refused. The message is safe to show to the person."""

    def __init__(self, message: str, *, needs_totp: bool = False, retry_after: int = 0):
        super().__init__(message)
        self.needs_totp, self.retry_after = needs_totp, retry_after


@dataclass(frozen=True)
class Session:
    user_id: int
    username: str
    csrf: str
    cmd_key: bytes
    token_hash: str


def _sha(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def totp_code(secret: bytes, step: int) -> str:
    """RFC 6238 (HMAC-SHA1, 30 s steps, 6 digits)."""
    digest = hmac.new(secret, struct.pack(">Q", step), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    number = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
    return f"{number % 1_000_000:06d}"


class Accounts:
    def __init__(self, store: Store, cfg: Config, clock=time.time):
        self.store, self.cfg, self._clock = store, cfg, clock
        self._db, self._mutex, self._vault = store.db, store.mutex, store.vault
        with self._mutex:
            self._db.executescript(SCHEMA)
        self._fails: dict[str, tuple[int, float]] = {}    # username -> (failures, paused until)
        self._nonces: dict[str, float] = {}                # used command nonces -> expiry

    # ---- users ------------------------------------------------------------
    def add_user(self, username: str, password: str) -> int:
        username = username.strip()
        if not (2 <= len(username) <= 32) or not username.replace("_", "").replace("-", "").isalnum():
            raise ValueError("username must be 2-32 letters, digits, - or _")
        self._check_password(password)
        with self._mutex, self._db:
            if self._db.execute("SELECT 1 FROM users WHERE username = ?", (username,)).fetchone():
                raise ValueError(f"user '{username}' already exists")
            cur = self._db.execute(
                "INSERT INTO users (username, password_hash, created_at) VALUES (?, ?, ?)",
                (username, _hasher.hash(password), int(self._clock())))
        return cur.lastrowid

    def set_password(self, username: str, password: str) -> None:
        self._check_password(password)
        with self._mutex, self._db:
            uid = self._user_id(username)
            self._db.execute("UPDATE users SET password_hash = ? WHERE id = ?",
                             (_hasher.hash(password), uid))
            self._db.execute("DELETE FROM sessions WHERE user_id = ?", (uid,))   # sign out everywhere

    def remove_user(self, username: str) -> None:
        with self._mutex, self._db:
            self._db.execute("DELETE FROM users WHERE id = ?", (self._user_id(username),))

    def users(self) -> list[tuple[str, bool]]:
        with self._mutex:
            rows = self._db.execute("SELECT username, totp_secret FROM users ORDER BY id").fetchall()
        return [(r["username"], r["totp_secret"] is not None) for r in rows]

    def enable_totp(self, username: str) -> tuple[str, str]:
        """-> (secret to type into the authenticator app, otpauth:// link)."""
        secret = secrets.token_bytes(20)
        with self._mutex, self._db:
            uid = self._user_id(username)
            self._db.execute("UPDATE users SET totp_secret = ?, totp_last = 0 WHERE id = ?",
                             (self._vault.seal(secret, f"totp:{uid}".encode()), uid))
        text = base64.b32encode(secret).decode()
        link = f"otpauth://totp/Doorbell:{quote(username)}?secret={text}&issuer=Doorbell"
        return text, link

    def disable_totp(self, username: str) -> None:
        with self._mutex, self._db:
            self._db.execute("UPDATE users SET totp_secret = NULL WHERE id = ?",
                             (self._user_id(username),))

    # ---- login ------------------------------------------------------------
    def login(self, username: str, password: str, totp: str = "") -> tuple[str, Session]:
        """-> (session token for the cookie, session). Raises AuthError."""
        key = username.strip().lower()[:64]
        now = self._clock()
        fails, paused_until = self._fails.get(key, (0, 0.0))
        if now < paused_until:
            raise AuthError("Too many attempts. Try again later.",
                            retry_after=int(paused_until - now) + 1)
        with self._mutex:
            row = self._db.execute("SELECT * FROM users WHERE username = ?", (username.strip(),)).fetchone()
        ok = False
        try:                                      # always do one hash, so timing does not reveal
            ok = _hasher.verify(row["password_hash"] if row else _DUMMY_HASH, password) and row is not None
        except (VerificationError, InvalidHashError):
            ok = False
        if not ok:
            self._record_failure(key, fails, now)
            raise AuthError("Wrong username or password.")
        if row["totp_secret"] is not None:
            if not totp:
                raise AuthError("Enter the 6-digit code from your authenticator app.", needs_totp=True)
            if not self._totp_ok(row, totp, now):
                self._record_failure(key, fails, now)
                raise AuthError("Wrong or already used code.", needs_totp=True)
        self._fails.pop(key, None)
        token = secrets.token_urlsafe(32)
        csrf, cmd_key = secrets.token_urlsafe(32), secrets.token_bytes(32)
        th = _sha(token)
        with self._mutex, self._db:
            self._db.execute("DELETE FROM sessions WHERE created_at < ?",
                             (int(now - self.cfg.session_max_days * 86400),))
            self._db.execute(
                "INSERT INTO sessions (token_hash, user_id, csrf, cmd_key, created_at, seen_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (th, row["id"], csrf, self._vault.seal(cmd_key, b"session:" + th.encode()),
                 int(now), int(now)))
        return token, Session(row["id"], row["username"], csrf, cmd_key, th)

    def session(self, token: Optional[str]) -> Optional[Session]:
        if not token:
            return None
        th, now = _sha(token), self._clock()
        with self._mutex, self._db:
            r = self._db.execute(
                "SELECT s.*, u.username FROM sessions s JOIN users u ON u.id = s.user_id "
                "WHERE s.token_hash = ?", (th,)).fetchone()
            if r is None:
                return None
            if (now - r["created_at"] > self.cfg.session_max_days * 86400
                    or now - r["seen_at"] > self.cfg.session_idle_hours * 3600):
                self._db.execute("DELETE FROM sessions WHERE token_hash = ?", (th,))
                return None
            if now - r["seen_at"] > 60:
                self._db.execute("UPDATE sessions SET seen_at = ? WHERE token_hash = ?", (int(now), th))
        return Session(r["user_id"], r["username"], r["csrf"],
                       self._vault.open(r["cmd_key"], b"session:" + th.encode()), th)

    def logout(self, session: Session) -> None:
        with self._mutex, self._db:
            self._db.execute("DELETE FROM sessions WHERE token_hash = ?", (session.token_hash,))

    # ---- signed, single-use door commands (MH-10) ---------------------------
    def verify_command(self, session: Session, method: str, path: str, body: bytes,
                       stamp: str, nonce: str, signature: str) -> Optional[str]:
        """None if the command is genuine and fresh, else why it is rejected."""
        now = self._clock()
        try:
            sent = int(stamp)
        except ValueError:
            return "bad timestamp"
        if abs(now - sent) > self.cfg.command_window_seconds:
            return "command is too old"
        if not (16 <= len(nonce) <= 64):
            return "bad nonce"
        message = b"\n".join([method.encode(), path.encode(), stamp.encode(), nonce.encode(), body])
        expected = hmac.new(session.cmd_key, message, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, signature.lower()):
            return "signature does not match"
        with self._mutex:
            for old in [n for n, exp in self._nonces.items() if exp < now]:
                del self._nonces[old]
            tag = session.token_hash[:16] + nonce
            if tag in self._nonces:
                return "command was already used"
            self._nonces[tag] = now + 2 * self.cfg.command_window_seconds
        return None

    # ---- internals --------------------------------------------------------
    @staticmethod
    def _check_password(password: str) -> None:
        if len(password) < MIN_PASSWORD:
            raise ValueError(f"password must be at least {MIN_PASSWORD} characters")

    def _user_id(self, username: str) -> int:
        row = self._db.execute("SELECT id FROM users WHERE username = ?", (username.strip(),)).fetchone()
        if row is None:
            raise KeyError(f"no user '{username}'")
        return row["id"]

    def _record_failure(self, key: str, fails: int, now: float) -> None:
        fails += 1
        pause = 0.0
        if fails >= self.cfg.login_fail_limit:
            doublings = min(fails - self.cfg.login_fail_limit, 10)
            pause = min(self.cfg.login_pause_seconds * 2 ** doublings, 900)
        if len(self._fails) > 10_000:             # do not let a flood of made-up names eat memory
            self._fails.clear()
        self._fails[key] = (fails, now + pause)

    def _totp_ok(self, row, code: str, now: float) -> bool:
        code = code.strip().replace(" ", "")
        secret = self._vault.open(row["totp_secret"], f"totp:{row['id']}".encode())
        step = int(now // 30)
        for candidate in (step - 1, step, step + 1):          # allow for clock drift
            if candidate > row["totp_last"] and hmac.compare_digest(totp_code(secret, candidate), code):
                with self._mutex, self._db:                   # a code works once only
                    self._db.execute("UPDATE users SET totp_last = ? WHERE id = ?",
                                     (candidate, row["id"]))
                return True
        return False
