"""Scale comparison (``ic bench --scale N``) on a small generated world: both engines agree
with each other and with an in-memory BFS, the report gains a Scale section, and every
temporary graph and schema is dropped."""

from __future__ import annotations

import json

import psycopg
import pytest

from ic.bench import report as rpt
from ic.bench import scale
from ic.config import get_settings
from ic.graph import client

DSN = get_settings().postgres_dsn


def _pg_available() -> bool:
    try:
        psycopg.connect(DSN, connect_timeout=3).close()
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _pg_available(), reason="Postgres not reachable")


def test_bfs_hops_and_adjacency():
    from ic.bench.pg_twin import WorldRows

    w = WorldRows(
        depends_on=[("b", "a", 0, 100, 0), ("c", "b", 0, 100, 0), ("d", "c", 200, 300, 0)],
        observed_calls=[("e", "a", 10**6, 0)],
    )
    adj = scale.impact_adjacency(w, 50)
    assert scale.bfs_hops(adj, "a") == {"b": 1, "e": 1, "c": 2}
    assert scale.bfs_hops(adj, "c") == {}


async def test_measure_scale_agrees_and_cleans_up(tmp_path):
    client.reset_client()
    res = await scale.measure_scale(40, seed=7, iterations=6, log=lambda *_: None)
    assert "error" not in res, res
    assert res["counts"]["services"] > 40  # services span regions
    assert set(res["ops"]) == {"closure_d2", "closure_d4", "closure_d8", "path"}
    for op in res["ops"].values():
        assert op["agreed"] == op["checked"] == res["iterations"] > 0
        assert op["reference"] == {"falkordb": op["checked"], "postgres": op["checked"]}
        assert 0 <= op["falkordb"]["p50_ms"] <= op["falkordb"]["p95_ms"]
    d = res["ops"]
    assert (
        d["closure_d2"]["mean_size"] <= d["closure_d4"]["mean_size"] <= d["closure_d8"]["mean_size"]
    )

    db = client.get_db()
    assert not [g for g in await db.list_graphs() if g.startswith("b:scale:")]
    with psycopg.connect(DSN) as c:
        left = c.execute(
            "SELECT nspname FROM pg_namespace WHERE nspname LIKE 'twin_scale_n40_%'"
        ).fetchall()
    assert left == []

    # merge into an existing report: scale.json written, report.md gains the section
    (tmp_path / "report.json").write_text(json.dumps(_minimal_report()))
    scale.merge_into_report(tmp_path, res)
    assert json.loads((tmp_path / "scale.json").read_text())["ops"].keys() == res["ops"].keys()
    merged = json.loads((tmp_path / "report.json").read_text())
    assert merged["scale"]["counts"] == res["counts"]
    md = (tmp_path / "report.md").read_text()
    assert "## Scale: FalkorDB vs Postgres on a large world" in md
    assert "Upstream closure, depth 8" in md
    assert scale.existing_scale(tmp_path)["seed"] == 7
    assert "timings_s" not in json.dumps(rpt.strip_timings(merged))


def _minimal_report() -> dict:
    return {
        "meta": {
            "cases": 0,
            "n_seen": 0,
            "n_heldout": 0,
            "classes": [],
            "heldout_classes": [],
            "notes": [],
            "seed": 1,
            "case_seeds_digest": "x",
            "commit": "c",
            "llm": "none",
        },
        "summary": {},
        "per_class": {},
        "latency": None,
        "learning_curve": None,
    }
