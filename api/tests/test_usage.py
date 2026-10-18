"""Usage counters (ic.usage): daily metrics, hashed client keys, refusal counts."""

from __future__ import annotations

from ic import usage
from ic.sessions.limits import Limiter
from ic.store import Store

NOW = 1_790_000_000.0


def _metrics(store: Store) -> dict[str, int]:
    with store.conn() as c:
        return dict(c.execute("SELECT metric, n FROM usage_daily").fetchall())


def test_bump_counts_per_day_and_hashes_the_client(tmp_path):
    store = Store(tmp_path / "ic.sqlite3")
    usage.bump(store, "asks", ip="203.0.113.9", now=NOW)
    usage.bump(store, "asks", ip="203.0.113.9", now=NOW)
    usage.bump(store, "asks", ip="203.0.113.10", now=NOW)
    usage.bump(store, "asks", now=NOW + 86400)
    with store.conn() as c:
        days = c.execute("SELECT day, n FROM usage_daily ORDER BY day").fetchall()
        clients = c.execute("SELECT client, n FROM usage_clients ORDER BY n DESC").fetchall()
    assert [tuple(r) for r in days] == [(usage.day(NOW), 3), (usage.day(NOW + 86400), 1)]
    assert [r[1] for r in clients] == [2, 1]
    assert all("203.0.113" not in r[0] and len(r[0]) == 16 for r in clients)
    # The key is stable across Store instances (salt persisted in SQLite).
    usage._salts.clear()
    with store.conn() as c:
        assert usage.client_key(store, "203.0.113.9", c) == clients[0][0]


def test_limiter_counts_refusals(tmp_path):
    store = Store(tmp_path / "ic.sqlite3")
    lim = Limiter(store, max_sessions=10, global_runs_per_hour=100, proof_verifies_per_ip_hour=1)
    assert lim.session_ask_retry("sid", 1, now=NOW) is None
    assert lim.session_ask_retry("sid", 1, now=NOW + 1) is not None
    assert lim.allow_proof_verify("1.2.3.4", now=NOW)
    assert not lim.allow_proof_verify("1.2.3.4", now=NOW + 1)
    m = _metrics(store)
    assert m == {"refused:ask_session": 1, "refused:proof_verify_ip": 1}


def test_prune_drops_old_client_rows(tmp_path):
    store = Store(tmp_path / "ic.sqlite3")
    usage.bump(store, "sessions", ip="1.1.1.1", now=NOW - 40 * 86400)
    usage.bump(store, "sessions", ip="1.1.1.1", now=NOW)
    assert usage.prune(store, now=NOW) == 1
    with store.conn() as c:
        assert c.execute("SELECT count(*) FROM usage_clients").fetchone()[0] == 1
        assert c.execute("SELECT count(*) FROM usage_daily").fetchone()[0] == 2
