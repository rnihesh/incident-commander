"""`ic bench --attach-llm`: live-LLM runs merged into the published report."""

from __future__ import annotations

import json

import pytest

from ic.bench import live_llm
from ic.bench import report as rpt


def _summary(correct: int, n: int = 5) -> dict:
    return {
        "label": "graph (llm: m)",
        "n": n,
        "n_seen": 3,
        "n_heldout": 2,
        "correct": correct,
        "accuracy_seen": 1.0,
        "accuracy_heldout": 0.5,
        "accuracy_overall": correct / n,
        "mean_ms": 1200.0,
        "mean_llm_tokens": 900.0,
        "mean_prompt_tokens_est": 0,
        "mean_tool_calls": 7.0,
        "errors": 0,
        "truncated": 0,
    }


def _fake(model: str, cases: int = 5, correct: int = 4) -> dict:
    return {
        "meta": {
            "cases": cases,
            "n_seen": 3,
            "n_heldout": 2,
            "classes": ["bad_deploy", "expiry"],
            "heldout_classes": ["expiry"],
            "approaches": [{"name": "graph", "label": "graph"}],
            "llm": model,
            "commit": "abcdef1234567890",
            "generated_at": "2026-10-09T10:00:00Z",
            "seed": 42,
            "case_seeds": [1, 2],
            "case_seeds_digest": "x",
            "notes": [],
        },
        "summary": {"graph": _summary(correct, cases)},
        "per_class": {
            "graph": {"bad_deploy": {"n": 3, "correct": 3}, "expiry": {"n": 2, "correct": 1}}
        },
        "results": [{"case": "c000", "root": "change:secret-root"}],
    }


def _main_report() -> dict:
    r = _fake("none (deterministic)", cases=50, correct=50)
    r["summary"]["graph"].update(label="graph", n=50)
    r["meta"]["notes"] = ["n"]
    return r


def _setup(tmp_path):
    out = tmp_path / "out"
    rpt.write_report(out, _main_report())
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "report.json").write_text(json.dumps(_fake("model-a")))
    (tmp_path / "b.json").write_text(json.dumps(_fake("model-b", correct=2)))
    return out


def test_attach_dir_and_json_idempotent(tmp_path):
    out = _setup(tmp_path)
    srcs = [str(tmp_path / "a"), str(tmp_path / "b.json")]
    live_llm.attach(out, srcs, log=lambda *_: None)
    first = (out / "report.json").read_text()
    live_llm.attach(out, srcs, log=lambda *_: None)
    assert (out / "report.json").read_text() == first
    rep = json.loads(first)
    runs = rep["live_llm_runs"]
    assert [r["model"] for r in runs] == ["model-a", "model-b"]
    a = runs[0]
    assert a["cases"] == 5 and a["n_seen"] == 3 and a["n_heldout"] == 2
    assert a["commit"] == "abcdef1234567890" and a["generated_at"]
    assert a["summary"]["graph"]["correct"] == 4
    assert a["per_class"]["graph"]["expiry"] == {"n": 2, "correct": 1}
    assert "results" not in a and "secret-root" not in json.dumps(runs)
    assert rep["meta"]["llm"].startswith("none")  # headline untouched
    md = (out / "report.md").read_text()
    assert "## Live LLM runs" in md and "model-b" in md


def test_reattach_replaces_same_model_and_cases(tmp_path):
    out = _setup(tmp_path)
    live_llm.attach(out, [str(tmp_path / "a")], log=lambda *_: None)
    (tmp_path / "a" / "report.json").write_text(json.dumps(_fake("model-a", correct=5)))
    live_llm.attach(out, [str(tmp_path / "a")], log=lambda *_: None)
    runs = json.loads((out / "report.json").read_text())["live_llm_runs"]
    assert len(runs) == 1 and runs[0]["summary"]["graph"]["correct"] == 5


def test_smoke_does_not_replace_full_run(tmp_path):
    out = _setup(tmp_path)
    (tmp_path / "full.json").write_text(json.dumps(_fake("model-a", cases=50, correct=40)))
    live_llm.attach(out, [str(tmp_path / "full.json"), str(tmp_path / "a")], log=lambda *_: None)
    runs = json.loads((out / "report.json").read_text())["live_llm_runs"]
    assert [(r["model"], r["cases"]) for r in runs] == [("model-a", 50), ("model-a", 5)]


def test_rejects_deterministic_and_missing(tmp_path):
    out = _setup(tmp_path)
    (tmp_path / "d.json").write_text(json.dumps(_main_report()))
    with pytest.raises(ValueError):
        live_llm.attach(out, [str(tmp_path / "d.json")], log=lambda *_: None)
    with pytest.raises(FileNotFoundError):
        live_llm.attach(tmp_path / "none", [str(tmp_path / "a")], log=lambda *_: None)


def test_md_absent_without_runs():
    assert rpt.live_llm_md(None) == []


def test_fallback_counts_degraded_verdicts_per_approach(tmp_path):
    r = _fake("model-c", cases=3, correct=3)
    r["results"] = [
        {"approach": "graph", "notes": {"degraded": True}},
        {"approach": "graph", "notes": {"degraded": False}},
        {"approach": "graph", "notes": {"degraded": True}},
    ]
    (tmp_path / "c.json").write_text(json.dumps(r))
    entry = live_llm.run_entry(tmp_path / "c.json")
    assert entry["summary"]["graph"]["fallback"] == 2
    assert "| 2/3 |" in "\n".join(rpt.live_llm_md([entry]))
    plain = live_llm.run_entry(_write(tmp_path, _fake("model-d")))
    assert "fallback" not in plain["summary"]["graph"]


def _write(tmp_path, report):
    p = tmp_path / f"{report['meta']['llm']}.json"
    p.write_text(json.dumps(report))
    return p
