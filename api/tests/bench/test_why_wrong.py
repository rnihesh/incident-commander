"""`ic bench --why-wrong` (R3, AE1, KTD4): every wrong live-LLM prediction re-checked with
verify_claim on its case world regenerated from the seed that case actually used."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from ic.bench import live_llm, why_wrong
from ic.bench import report as rpt
from ic.graph import client
from ic.store import Store
from ic.tools.claims import verify_claim

RESULTS = Path(__file__).resolve().parents[2] / "bench_results"
OPUS = RESULTS / "llm-claude-opus-5-5" / "report.json"
CLAUSES = {
    "after_onset",
    "origin",
    "coverage",
    "order",
    "signature",
    "outside_window",
    "not_a_change",
}


def _c000() -> dict:
    """The committed Opus 5.5 run, cut down to the wrong predictions on case c000 (full context
    and vector RAG; the graph agent got it right)."""
    r = json.loads(OPUS.read_text())
    r["results"] = [x for x in r["results"] if x["case"] == "c000" and not x["correct"]]
    return r


@pytest.fixture
def touched(monkeypatch):
    """Every (run, sid) the step regenerates, so leaks are checked against our own graphs
    only (other workers share this FalkorDB)."""
    client.reset_client()
    seen: list[tuple[str, str]] = []
    real = why_wrong.setup_case

    async def recording(run, plan, store):
        view, root, seed = await real(run, plan, store)
        seen.append((run, view.sid))
        return view, root, seed

    monkeypatch.setattr(why_wrong, "setup_case", recording)
    return seen


async def _leaks(seen: list[tuple[str, str]]) -> list[str]:
    prefixes = [p for run, sid in seen for p in (f"b:{run}:", f"s:{sid}:")]
    return [g for g in await client.get_db().list_graphs() if g.startswith(tuple(prefixes))]


async def test_ae1_c000_refutes_prediction_and_verifies_root(touched, tmp_path):
    """AE1: c000 regenerates with root chg-05690; Opus's chg-05692 is REFUTED on a clause."""
    rows = (await why_wrong.explain_runs([_c000()], log=lambda *_: None))[0]
    assert len(touched) == 1  # one regeneration serves both approaches
    assert {r["approach"] for r in rows} == {"full_context", "vector_rag"}
    for r in rows:
        assert r["case"] == "c000" and r["predicted"] == "chg-05692"
        assert r["fault_class"] == "undeclared_coupling" and r["held_out"] is True
        assert r["verdict"] == "REFUTED" and r["clause"] in CLAUSES
        assert r["detail"] and "MATCH" in r["query_text"]
    assert "root_id" not in json.dumps(rows) and "chg-05690" not in json.dumps(rows)

    case = next(c for c in _c000()["case_list"] if c["case"] == "c000")
    store = Store(tmp_path / "s.sqlite3")
    async with why_wrong.regenerated(case, store) as (view, root):
        assert root == "chg-05690"
        res = (await verify_claim(view.snapshot_known, view.incident_id, root)).result
        assert res["verdict"] == "VERIFIED"
    assert await _leaks(touched) == []


async def test_no_claim_needs_no_regeneration(monkeypatch):
    r = _c000()
    for x in r["results"]:
        x["predicted"] = None

    async def boom(*_a, **_k):
        raise AssertionError("no regeneration for a prediction without an id")

    monkeypatch.setattr(why_wrong, "setup_case", boom)
    rows = (await why_wrong.explain_runs([r], log=lambda *_: None))[0]
    assert [(x["verdict"], x["predicted"], x["clause"]) for x in rows] == [
        ("no_claim", None, None),
        ("no_claim", None, None),
    ]


async def test_root_mismatch_cannot_regenerate(touched):
    r = _c000()
    for c in r["case_list"]:
        c["root_id"] = "chg-00000"
    rows = (await why_wrong.explain_runs([r], log=lambda *_: None))[0]
    assert len(touched) == 1
    assert {x["verdict"] for x in rows} == {"cannot_regenerate"}
    assert all(x["clause"] is None and x["query_text"] is None for x in rows)
    assert await _leaks(touched) == []


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("alert_time", 1),
        ("alert_service", "svc-elsewhere@us-east"),
        ("incident_id", "inc-999"),
    ],
)
async def test_world_fingerprint_mismatch_cannot_regenerate(touched, field, value):
    """Root ids are sequential, so a matching root alone does not prove the same world: the
    seed-dependent alert time, alert service and incident id must match the stored row too."""
    r = _c000()
    for c in r["case_list"]:
        if c["case"] == "c000":
            c[field] = value
    rows = (await why_wrong.explain_runs([r], log=lambda *_: None))[0]
    assert len(touched) == 1
    assert {x["verdict"] for x in rows} == {"cannot_regenerate"}
    assert all(field in x["detail"] for x in rows)
    assert all(x["clause"] is None and x["query_text"] is None for x in rows)
    assert await _leaks(touched) == []


async def test_verified_wrong_prediction_is_inconsistent(touched):
    """A prediction graded wrong that verify_claim VERIFIES contradicts the grade: it is
    flagged, never published as an explanation."""
    r = _c000()
    for x in r["results"]:
        x["predicted"] = "chg-05690"  # the regenerated root, still graded wrong
    rows = (await why_wrong.explain_runs([r], log=lambda *_: None))[0]
    assert {x["verdict"] for x in rows} == {"inconsistent"}
    assert all("disagrees with the grade" in x["detail"] for x in rows)
    assert all(x["clause"] is None and x["query_text"] is None for x in rows)
    assert await _leaks(touched) == []


async def test_correct_rows_and_missing_seed(monkeypatch):
    r = _c000()
    wrong = r["results"][0]
    r["results"] = [
        {**wrong, "approach": "graph", "correct": True},  # right answers are not explained
        {**wrong, "case": "c999"},  # no seed in case_list
    ]

    async def boom(*_a, **_k):
        raise AssertionError("unexpected regeneration")

    monkeypatch.setattr(why_wrong, "setup_case", boom)
    rows = (await why_wrong.explain_runs([r], log=lambda *_: None))[0]
    assert [(x["case"], x["verdict"]) for x in rows] == [("c999", "cannot_regenerate")]


def _out(tmp_path) -> tuple[Path, Path]:
    """A main report with the cut-down Opus run attached from a per-run directory."""
    out = tmp_path / "out"
    main = json.loads((RESULTS / "report.json").read_text())
    main.pop("live_llm_runs", None)
    rpt.write_report(out, main)
    src = out / "llm-opus"
    src.mkdir()
    (src / "report.json").write_text(json.dumps(_c000()))
    live_llm.attach(out, [str(src)], log=lambda *_: None)
    return out, src


async def test_attach_why_wrong_idempotent_and_survives_reattach(touched, tmp_path):
    out, src = _out(tmp_path)
    await why_wrong.attach(out, log=lambda *_: None)
    first = (out / "report.json").read_text()
    run = json.loads(first)["live_llm_runs"][0]
    assert run["source"] == "llm-opus/report.json"
    assert len(run["why_wrong"]) == 2
    md = (out / "report.md").read_text()
    assert "## Why it was wrong" in md and "chg-05692" in md

    await why_wrong.attach(out, log=lambda *_: None)
    assert (out / "report.json").read_text() == first

    live_llm.attach(out, [str(src)], log=lambda *_: None)  # same run: why_wrong is kept
    assert (out / "report.json").read_text() == first

    changed = copy.deepcopy(_c000())
    changed["meta"]["generated_at"] = "2030-01-01T00:00:00Z"
    (src / "report.json").write_text(json.dumps(changed))
    live_llm.attach(out, [str(src)], log=lambda *_: None)  # a new run: stale rows dropped
    assert "why_wrong" not in json.loads((out / "report.json").read_text())["live_llm_runs"][0]
    assert await _leaks(touched) == []


def test_md_section_absent_without_rows():
    assert rpt.why_wrong_md(None) == []
    assert rpt.why_wrong_md([{"model": "m", "cases": 1, "summary": {}}]) == []
