"""Learning-curve smoke test (U13, R21, F3): two incidents downstream of one coupling."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from ic.bench import learning_curve as lc
from ic.bench import report as rpt
from ic.graph import client
from ic.tools.recall import MIN_CONFIDENCE

REPORT = Path(__file__).resolve().parents[2] / "bench_results" / "report.json"


async def test_two_incident_learning_curve_writes_report(tmp_path):
    client.reset_client()
    if REPORT.exists():
        shutil.copy(REPORT, tmp_path / "report.json")
    res = await lc.run_learning_curve(runs=2, seed=42, out=tmp_path, log=lambda *_: None)
    first, second = res["incidents"]
    assert first["verdict"] == second["verdict"] == "DIAGNOSED"
    assert first["correct"] and second["correct"]
    assert second["recall_used"] and second["memory_seeded"] and second["coupling_confirmed"]
    assert second["tool_calls"] < first["tool_calls"]  # F3
    assert first["selected"] is True and "selected" not in second
    assert isinstance(res["meta"]["discarded_incident1_attempts"], list)
    assert "retry-until-correct" in res["meta"]["method"]
    c = res["meta"]["coupling"]
    assert any(
        e["caller"] == c["caller"] and e["callee"] == c["callee"] and e["action"] == "created"
        for e in first["learned"]
    )
    s = res["summary"]
    assert s["first_tool_calls"] == first["tool_calls"] and s["reduction_pct"] > 0
    saved = json.loads((tmp_path / "learning_curve.json").read_text())
    assert saved["summary"] == json.loads(json.dumps(s))
    if REPORT.exists():
        report = json.loads((tmp_path / "report.json").read_text())
        assert report["learning_curve"]["meta"]["coupling"] == c
        md = (tmp_path / "report.md").read_text()
        assert "## Learning curve" in md and "Not yet measured" not in md
        assert f"incident 1 = {first['tool_calls']}" in md
    # no graphs of the run are left behind
    names = await client.get_db().list_graphs()
    assert not [n for n in names if n.endswith(":storm:probe")]
    client.reset_client()


async def test_forget_phase_halves_confidence_until_recall_stops_seeding(tmp_path):
    """AE4 (R8): after the coupling is removed from the world, each contradicting incident
    halves the learned edge's confidence until it is under MIN_CONFIDENCE; the next incident
    is not seeded by it. Rewriting report.json keeps the live LLM runs and their why-wrong."""
    client.reset_client()
    report = json.loads(REPORT.read_text()) if REPORT.exists() else {}
    report.setdefault("live_llm_runs", [{"label": "m", "why_wrong": {"rows": [{"x": 1}]}}])
    assert any("why_wrong" in r for r in report["live_llm_runs"])
    (tmp_path / "report.json").write_text(json.dumps(report))
    res = await lc.run_learning_curve(runs=2, forget=4, seed=42, out=tmp_path, log=lambda *_: None)
    rows = res["incidents"]
    c = res["meta"]["coupling"]
    learn = [r for r in rows if r["phase"] == "learn"]
    forget = [r for r in rows if r["phase"] == "forget"]
    assert len(learn) == 2 and forget
    # every row carries the edge's confidence, touched or not
    assert all(isinstance(r["edge_confidence"], float) for r in rows)
    peak = learn[-1]["edge_confidence"]
    assert peak == pytest.approx(0.8)
    *contra, after = forget
    assert 2 <= len(contra) <= 3  # 0.8 -> 0.4 -> 0.2
    conf = peak
    for r in forget:
        assert r["contradicted"] and r["edge_action"] == "contradicted"
        assert r["edge_confidence_before"] == pytest.approx(conf)
        conf *= 0.5
        assert r["edge_confidence"] == pytest.approx(conf)
        assert "RESOLVED" in r["states"] and r["final_state"] == "LEARNED"
        assert c["caller"] in r["symptomatic"] and c["callee"] in r["symptomatic"]
    assert [r["edge_confidence"] < MIN_CONFIDENCE for r in contra][-1]
    assert all(r["edge_confidence_before"] >= MIN_CONFIDENCE for r in contra)
    # one more incident after the fall: recall no longer seeds the old suspect
    assert after["check"] and not any(r["check"] for r in contra)
    assert after["edge_confidence_before"] < MIN_CONFIDENCE
    assert not after["edge_seeded"] and not after["memory_seeded"]
    assert learn[1]["edge_seeded"]  # while confident, the edge did seed recall
    f = res["meta"]["forget"]
    assert f["threshold"] == MIN_CONFIDENCE and f["contradictions"] == len(forget)
    assert f["check_incident"] == after["incident_id"]
    assert f["change"]["service_id"] == c["caller"]
    assert res["summary"]["n"] == 2  # the F3 summary covers the learn phase only
    saved = json.loads((tmp_path / "report.json").read_text())
    assert saved["live_llm_runs"] == report["live_llm_runs"]
    assert saved["learning_curve"]["meta"]["forget"]["contradictions"] == len(forget)
    md = (tmp_path / "report.md").read_text()
    assert "contradicted" in md
    names = await client.get_db().list_graphs()
    assert not [n for n in names if n.endswith(":storm:probe")]
    client.reset_client()


def test_learning_curve_section_placeholder_when_absent():
    assert rpt.learning_curve_md(None) == ["_Not yet measured (run `ic learn`)._"]
