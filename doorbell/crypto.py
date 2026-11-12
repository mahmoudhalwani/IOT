"""Encryption at rest (MH-11): AES-256-GCM with a random 96-bit nonce per record.

The master key lives in a file readable only by the service user. This protects
the database and clips if they are copied off the device or backed up; it does
not protect against someone who steals the whole unlocked SD card *and* the key
file, which is a stated limit of the prototype.
"""
from __future__ import annotations

import os
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

NONCE_LEN = 12


def load_or_create_key(path: Path) -> bytes:
    if path.exists():
        key = path.read_bytes()
        if len(key) != 32:
            raise ValueError(f"{path} is not a 32-byte key")
        return key
    path.parent.mkdir(parents=True, exist_ok=True)
    key = AESGCM.generate_key(bit_length=256)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(key)
    return key


class Vault:
    def __init__(self, key: bytes):
        self._aead = AESGCM(key)

    def seal(self, plaintext: bytes, context: bytes) -> bytes:
        """`context` binds the ciphertext to its owner (e.g. b"template:7") so a
        blob moved to another row fails to decrypt."""
        nonce = os.urandom(NONCE_LEN)
        return nonce + self._aead.encrypt(nonce, plaintext, context)

    def open(self, blob: bytes, context: bytes) -> bytes:
        return self._aead.decrypt(blob[:NONCE_LEN], blob[NONCE_LEN:], context)
