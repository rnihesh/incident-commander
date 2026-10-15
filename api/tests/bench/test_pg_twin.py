"""Postgres twin (U15, R29): the same small world in FalkorDB and Postgres gives the same
closure sets and shortest-path lengths (valid-time filtered), and the same nearest
postmortem."""

from __future__ import annotations

import random

import psycopg
import pytest

from ic.bench import pg_twin
from ic.config import get_settings
from ic.graph import client, naming
from ic.graph.schema import WORLD_NOW
from ic.graph.snapshots import build_view
from ic.world.embed import HashingEmbedder
from ic.world.generator import generate_world
from ic.world.loader import load_world

DSN = get_settings().postgres_dsn
T_PAST = WORLD_NOW - 45 * 86400  # history dependencies make the topology differ by time


def _pg_available() -> bool:
    try:
        psycopg.connect(DSN, connect_timeout=3).close()
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _pg_available(), reason="Postgres not reachable")


@pytest.fixture(scope="module")
async def twin_world():
    client.reset_client()
    run = "t" + naming.new_session_id()[:10]
    world = generate_world(7, 40)
    graph = naming.bench(run, "pgtwin", "world")
    await load_world(world, graph, embedder=HashingEmbedder(512))
    views = {}
    for t in (WORLD_NOW, T_PAST):
        for view in ("true", "known"):
            name = naming.bench(run, "pgtwin", f"{view}-{t}")
            await build_view(graph, name, t, view)
            views[(t, view)] = name
    rows = await pg_twin.read_world(graph)
    twin = pg_twin.PgTwin(DSN, f"twin_{run}")
    twin.load(rows)
    yield world, graph, views, twin, rows
    twin.drop()
    db = client.get_db()
    for name in await db.list_graphs():
        if name.startswith(f"b:{run}:"):
            await db.select_graph(name).delete()


async def test_loaded_counts_match_the_graph(twin_world):
    world, _, _, twin, _ = twin_world
    c = twin.counts()
    assert c["services"] == len(world.services)
    assert c["depends_on"] == len(world.dependencies)
    assert c["observed_calls"] == len(world.observed_calls)
    assert c["changes"] == len(world.changes)
    assert c["postmortems"] == len(world.postmortems)


@pytest.mark.parametrize("t", [WORLD_NOW, T_PAST])
@pytest.mark.parametrize("view", ["true", "known"])
async def test_closure_equals_falkordb_for_every_service(twin_world, t, view):
    world, _, views, twin, _ = twin_world
    snap = views[(t, view)]
    nonempty = 0
    for s in world.services:
        f = await pg_twin.falkor_closure(snap, s.id)
        p = twin.closure(s.id, t, known=view == "known")
        assert f == p, s.id
        nonempty += bool(f)
    assert nonempty > len(world.services) // 4  # the comparison is not vacuous


@pytest.mark.parametrize("t", [WORLD_NOW, T_PAST])
async def test_path_lengths_match(twin_world, t):
    world, _, views, twin, _ = twin_world
    snap = views[(t, "true")]
    rng = random.Random(3)
    ids = sorted(s.id for s in world.services)
    found = 0
    for _ in range(150):
        a, b = rng.sample(ids, 2)
        f = await pg_twin.falkor_path_length(snap, a, b)
        p = twin.path_length(a, b, t)
        assert f == p, (a, b, f, p)
        found += f is not None
    # and for reachable pairs drawn from the closure
    for a in ids[:40]:
        reach = sorted(await pg_twin.falkor_closure(snap, a))
        for b in reach[:3]:
            f = await pg_twin.falkor_path_length(snap, a, b)
            assert f is not None and f == twin.path_length(a, b, t), (a, b)
            found += 1
    assert found > 20


async def test_nearest_postmortem_agrees(twin_world):
    _, _, views, twin, rows = twin_world
    snap = views[(WORLD_NOW, "true")]
    for pid, vec in rows.postmortems[:15]:
        f = await pg_twin.falkor_topk(snap, vec, 3)
        p = twin.topk(vec, 3)
        assert f[0][0] == pid and p[0][0] == pid
        assert f[0][1] == pytest.approx(0.0, abs=1e-4)
        assert p[0][1] == pytest.approx(0.0, abs=1e-4)


async def test_compare_reports_agreement_and_percentiles(twin_world):
    _, _, views, twin, _ = twin_world
    res = await pg_twin.compare(views[(WORLD_NOW, "true")], twin, WORLD_NOW, iterations=8, seed=1)
    for op in ("closure", "path", "topk"):
        o = res["ops"][op]
        assert o["falkordb"]["n"] == 8 and o["postgres"]["n"] == 8
        assert 0 <= o["falkordb"]["p50_ms"] <= o["falkordb"]["p95_ms"]
        assert 0 <= o["postgres"]["p50_ms"] <= o["postgres"]["p95_ms"]
    assert res["ops"]["closure"]["agreed"] == res["ops"]["closure"]["checked"]
    assert res["ops"]["path"]["agreed"] == res["ops"]["path"]["checked"]
    assert "exact" in res["ops"]["topk"]


def test_schema_name_is_validated():
    with pytest.raises(ValueError):
        pg_twin.PgTwin(DSN, "bad; DROP TABLE x")
