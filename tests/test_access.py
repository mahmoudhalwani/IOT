from datetime import datetime, time

import pytest

from doorbell.access import AccessRule, parse_days, parse_hours

MON_10 = datetime(2026, 11, 9, 10, 0)      # a Monday


def test_default_is_always():
    assert AccessRule().check(MON_10) == (True, "ok")


def test_date_range():
    rule = AccessRule(valid_from=datetime(2026, 11, 9, 9), valid_until=datetime(2026, 11, 9, 11))
    assert rule.check(MON_10)[0]
    assert rule.check(datetime(2026, 11, 9, 8, 59)) == (False, "access has not started yet")
    assert rule.check(datetime(2026, 11, 9, 11, 1)) == (False, "access has expired")


def test_hours_and_days():
    rule = AccessRule(days=parse_days("mon,thu"), start=time(9), end=time(12))
    assert rule.check(MON_10)[0]
    assert not rule.check(MON_10.replace(hour=13))[0]
    assert not rule.check(datetime(2026, 11, 10, 10))[0]      # Tuesday


def test_overnight_window_belongs_to_start_day():
    rule = AccessRule(days=parse_days("mon"), start=time(22), end=time(6))
    assert rule.check(datetime(2026, 11, 9, 23))[0]           # Monday night
    assert rule.check(datetime(2026, 11, 10, 5))[0]           # ...into Tuesday morning
    assert not rule.check(datetime(2026, 11, 10, 23))[0]      # Tuesday night
    assert not rule.check(datetime(2026, 11, 9, 5))[0]        # Monday morning = Sunday's window
    assert not rule.check(datetime(2026, 11, 9, 12))[0]


def test_parsers():
    assert parse_hours("08:00-18:30") == (time(8), time(18, 30))
    with pytest.raises(ValueError):
        parse_days("mon,funday")
