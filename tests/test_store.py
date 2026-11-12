import sqlite3

import numpy as np
import pytest
from cryptography.exceptions import InvalidTag

from conftest import unit
from doorbell.access import AccessRule, parse_days


def file_bytes(cfg):
    return b"".join(p.read_bytes() for p in cfg.data_dir.glob("doorbell.db*"))


def test_consent_is_required(store):
    with pytest.raises(ValueError):
        store.add_person("Sara", consent=False)
    assert store.people() == []


def test_template_roundtrip_and_encrypted_on_disk(store, cfg):
    pid = store.add_person("Sara", consent=True)
    vec = unit(1)
    store.add_template(pid, vec)
    (got_pid, got), = store.templates()
    assert got_pid == pid and np.array_equal(got, vec)
    assert vec.tobytes()[:32] not in file_bytes(cfg)
    assert oct(cfg.key_path.stat().st_mode & 0o777) == "0o600"


def test_remove_deletes_face_data(store, cfg):
    pid = store.add_person("Sara", consent=True)
    tid = store.add_template(pid, unit(1))
    blob = sqlite3.connect(cfg.db_path).execute(
        "SELECT blob FROM templates WHERE id = ?", (tid,)).fetchone()[0]
    assert blob[:24] in file_bytes(cfg)
    store.remove_person(pid)
    assert store.people() == [] and store.templates() == []
    assert blob[:24] not in file_bytes(cfg)          # overwritten, not just unlinked


def test_blob_cannot_be_moved_to_another_person(store, cfg):
    a = store.add_person("A", consent=True)
    b = store.add_person("B", consent=True)
    store.add_template(a, unit(1))
    db = sqlite3.connect(cfg.db_path)
    db.execute("UPDATE templates SET person_id = ?", (b,))
    db.commit()
    with pytest.raises(InvalidTag):
        store.templates()


def test_disabled_people_are_not_matched(store):
    pid = store.add_person("Sara", consent=True)
    store.add_template(pid, unit(1))
    store.set_enabled(pid, False)
    assert store.templates() == []


def test_access_rule_is_saved(store):
    rule = AccessRule(days=parse_days("mon,fri"))
    pid = store.add_person("Guest", "guest", consent=True, rule=rule)
    assert store.person(pid).rule == rule
    store.set_access(pid, AccessRule())
    assert store.person(pid).rule == AccessRule()
    with pytest.raises(KeyError):
        store.remove_person(999)
