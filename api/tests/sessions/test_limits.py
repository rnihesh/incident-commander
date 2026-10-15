import sqlite3

import pytest

from ic.sessions.limits import Limiter
from ic.store import Store


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "l.sqlite3")


def add_session(store, sid, ip, now=1000.0, active=0):
    with store.conn() as c:
        c.execute("INSERT INTO sessions VALUES (?, ?, ?, ?, ?)", (sid, ip, now, now, active))


def test_fourth_run_per_session_rejected(store):
    lim = Limiter(store, max_sessions=8, global_runs_per_hour=60)
    add_session(store, "s1", "1.1.1.1")
    for i in range(3):
        assert lim.check_run("s1", "1.1.1.1", now=1000 + i).allowed
        lim.record_run("s1", "1.1.1.1", now=1000 + i)
    d = lim.check_run("s1", "1.1.1.1", now=1010)
    assert not d.allowed and "3 runs per hour" in d.reason and d.fallback == "recorded_run"
    assert lim.check_run("s1", "1.1.1.1", now=1000 + 3700).allowed  # window slides


def test_concurrent_run_rejected(store):
    lim = Limiter(store)
    add_session(store, "s1", "1.1.1.1", active=1)
    assert not lim.check_run("s1", "1.1.1.1", now=1000).allowed


def test_cookieless_loop_per_ip(store):
    lim = Limiter(store, max_sessions=100)
    # fresh session each time: 6 runs/hour per IP stops it
    for i in range(6):
        add_session(store, f"s{i}", "9.9.9.9")
        assert lim.check_run(f"s{i}", "9.9.9.9", now=1000 + i).allowed
        lim.record_run(f"s{i}", "9.9.9.9", now=1000 + i)
    add_session(store, "s7", "9.9.9.9")
    d = lim.check_run("s7", "9.9.9.9", now=1100)
    assert not d.allowed and d.fallback == "recorded_run"
    assert lim.check_run("s7", "8.8.8.8", now=1100).allowed


def test_ip_concurrent_sessions_and_creations(store):
    lim = Limiter(store, max_sessions=100, idle_ttl_s=1800)
    add_session(store, "a", "1.1.1.1")
    assert lim.check_session_create("1.1.1.1", now=1001).allowed
    add_session(store, "b", "1.1.1.1")
    d = lim.check_session_create("1.1.1.1", now=1001)
    assert not d.allowed and d.fallback == "recorded_run"
    assert lim.check_session_create("1.1.1.1", now=1000 + 5000).allowed  # idle ones don't count
    for i in range(6):
        lim.record_session_create("3.3.3.3", now=1000 + i)
    assert not lim.check_session_create("3.3.3.3", now=1100).allowed


def test_ninth_session_falls_back(store):
    lim = Limiter(store, max_sessions=8)
    for i in range(8):
        add_session(store, f"s{i}", f"10.0.0.{i}")
    d = lim.check_session_create("10.0.0.99", now=1001)
    assert not d.allowed and d.fallback == "recorded_run"


def test_global_ceiling(store):
    lim = Limiter(store, global_runs_per_hour=2)
    for i in range(3):
        add_session(store, f"s{i}", f"10.0.0.{i}")
    for i in range(2):
        lim.record_run(f"s{i}", f"10.0.0.{i}", now=1000)
    d = lim.check_run("s2", "10.0.0.2", now=1001)
    assert not d.allowed and "global" in d.reason


def test_survives_restart(tmp_path):
    p = tmp_path / "r.sqlite3"
    s1 = Store(p)
    add_session(s1, "s1", "1.1.1.1")
    lim = Limiter(s1)
    for i in range(3):
        lim.record_run("s1", "1.1.1.1", now=1000 + i)
    assert not Limiter(Store(p)).check_run("s1", "1.1.1.1", now=1010).allowed
    assert sqlite3.connect(p)  # file persisted
