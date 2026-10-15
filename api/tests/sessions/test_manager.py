import pytest

from ic.graph import client, naming
from ic.sessions.manager import SessionManager
from ic.store import Store

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def mgr(tmp_path):
    client.reset_client()
    if not await client.graph_exists(naming.seed_world()):
        pytest.skip("seed:world not loaded")
    m = SessionManager(Store(tmp_path / "s.sqlite3"), max_sessions=8, idle_ttl_s=100)
    yield m
    db = client.get_db()
    mine = tuple(f"s:{s}:" for s in set(_sids(m)))
    for n in await db.list_graphs():
        if n.startswith(mine):
            await db.select_graph(n).delete()
    client.reset_client()


def _sids(m):
    with m.store.conn() as c:
        return [r[0] for r in c.execute("SELECT sid FROM sessions")] + list(_made)


_made: list[str] = []


async def mk(m, ip="1.1.1.1", now=1000.0):
    s = await m.create(ip, now=now)
    _made.append(s.sid)
    return s


async def count(g):
    return (await client.query(g, "MATCH (n) RETURN count(n)", read_only=True)).result_set[0][0]


async def test_get_rejects_malformed_and_unknown(mgr):
    s = await mk(mgr)
    assert mgr.get(s.sid).sid == s.sid
    for bad in (None, "", "abc", "../x", "A" * 32, s.sid + "0", "s:" + s.sid):
        assert mgr.get(bad) is None
    guessed = naming.new_session_id()  # well-formed but never created
    assert mgr.get(guessed) is None


async def test_create_copies_seed_and_tracks(mgr):
    s = await mk(mgr)
    world = naming.session_world(s.sid)
    assert await count(world) == await count(naming.seed_world())
    assert mgr.tracked_graphs(s.sid) == [world]
    assert mgr.count_active() >= 1


async def test_sessions_isolated(mgr):
    a, b = await mk(mgr), await mk(mgr, ip="2.2.2.2")
    wa, wb = naming.session_world(a.sid), naming.session_world(b.sid)
    before = await count(wb)
    await client.query(wa, "CREATE (:Injected {x: 1})")
    assert await count(wb) == before
    assert await count(wa) == before + 1
    assert await count(naming.seed_world()) == before


async def test_cleanup_idle_only_exact_tracked(mgr):
    idle, busy, fresh = await mk(mgr, now=1000), await mk(mgr, now=1000), await mk(mgr, now=1090)
    mgr.run_started(busy.sid)
    stray = f"s:{idle.sid}:stray"  # same prefix, never tracked
    await client.query(stray, "CREATE (:X)")
    removed = await mgr.cleanup(now=1150)
    assert removed == [naming.session_world(idle.sid)]
    assert not await client.graph_exists(naming.session_world(idle.sid))
    assert await client.graph_exists(stray)  # exact-name deletion only
    assert mgr.get(idle.sid) is None
    assert await client.graph_exists(naming.session_world(busy.sid))
    assert mgr.get(busy.sid) is not None
    assert mgr.get(fresh.sid) is not None
    mgr.run_finished(busy.sid)
    await mgr.cleanup(now=1150)
    assert mgr.get(busy.sid) is None
    await client.delete_graph(stray)


async def test_touch_prevents_cleanup(mgr):
    s = await mk(mgr, now=1000)
    mgr.touch(s.sid, now=1120)
    assert await mgr.cleanup(now=1150) == []


async def test_cleanup_continues_past_failing_delete(tmp_path, monkeypatch):
    from ic.sessions import manager as mod

    m = SessionManager(Store(tmp_path / "c.sqlite3"), max_sessions=8, idle_ttl_s=100)
    sids = [naming.new_session_id() for _ in range(3)]
    for sid in sids:
        with m.store.conn() as c:
            c.execute("INSERT INTO sessions VALUES (?, '1.1.1.1', 0, 0, 0)", (sid,))
        m.track_graph(sid, naming.session_world(sid))
    bad = naming.session_world(sids[1])

    async def fake_delete(name):
        if name == bad:
            raise RuntimeError("boom")

    monkeypatch.setattr(mod, "delete_graph", fake_delete)
    removed = await m.cleanup(now=1000.0)
    assert sorted(removed) == sorted(naming.session_world(s) for s in (sids[0], sids[2]))
    assert m.get(sids[0]) is None and m.get(sids[2]) is None
    assert m.get(sids[1]) is not None  # retained for retry
    assert m.tracked_graphs(sids[1]) == [bad]


async def test_cleanup_prunes_judge_challenges(tmp_path, monkeypatch):
    """An expired session's challenge budget rows go with it; live sessions keep theirs."""
    from ic.api import routes_challenge
    from ic.sessions import manager as mod

    async def fake_delete(name):
        return None

    monkeypatch.setattr(mod, "delete_graph", fake_delete)
    m = SessionManager(Store(tmp_path / "j.sqlite3"), max_sessions=8, idle_ttl_s=100)
    old, live = naming.new_session_id(), naming.new_session_id()
    with m.store.conn() as c:
        c.execute("INSERT INTO sessions VALUES (?, '1.1.1.1', 0, 0, 0)", (old,))
        c.execute("INSERT INTO sessions VALUES (?, '1.1.1.1', 0, 1100, 0)", (live,))
    for sid in (old, live):
        assert routes_challenge.spend(m.store, sid, "inc-001")
    await m.cleanup(now=1150)
    assert routes_challenge.remaining(m.store, old, "inc-001") == 2
    assert routes_challenge.remaining(m.store, live, "inc-001") == 1
    with m.store.conn() as c:
        sids = {r[0] for r in c.execute("SELECT sid FROM judge_challenges")}
    assert sids == {live}
