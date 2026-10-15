"""Attach live-LLM benchmark runs to the published report.

``ic bench --attach-llm <report_dir_or_json>`` reads a finished run (its ``report.json``, or a
single ``.json`` report file) and stores a compact entry in the main report's
``live_llm_runs`` list. Entries are keyed by (model, cases), so re-attaching a run replaces its
entry while a 5-case smoke never overwrites the 50-case run of the same model. Per-case rows
and root ids are never copied. ``source`` records where the run's own report lives (relative
to the output directory when it is inside it) for ``ic bench --why-wrong``; a re-attach of
the same run (same commit and timestamp) keeps its ``why_wrong`` rows, a new run drops them."""

from __future__ import annotations

import json
from pathlib import Path

from ic.bench import report as rpt

SUMMARY_KEYS = (
    "label",
    "n",
    "n_seen",
    "n_heldout",
    "correct",
    "accuracy_seen",
    "accuracy_heldout",
    "accuracy_overall",
    "mean_ms",
    "mean_llm_tokens",
    "mean_tool_calls",
    "errors",
)


def _load(src: str | Path) -> dict:
    p = Path(src)
    if p.is_dir():
        p = p / "report.json"
    data = json.loads(p.read_text())
    if not isinstance(data, dict) or "meta" not in data or "summary" not in data:
        raise ValueError(f"{p} is not a benchmark report")
    return data


def run_entry(src: str | Path) -> dict:
    r = _load(src)
    m = r["meta"]
    model = m.get("llm") or "unknown"
    if str(model).lower().startswith("none"):
        raise ValueError(f"{src} was not a live LLM run (llm={model!r})")
    summary = {name: {k: s[k] for k in SUMMARY_KEYS if k in s} for name, s in r["summary"].items()}
    # Verdicts the deterministic funnel produced after the model failed (timeout, step cap...):
    # those are not the model's answers, so the table shows them next to the score.
    for name, s in summary.items():
        rows = [x for x in r.get("results", []) if x.get("approach") == name]
        if any("degraded" in (x.get("notes") or {}) for x in rows):
            s["fallback"] = sum(1 for x in rows if (x.get("notes") or {}).get("degraded"))
    return {
        "model": model,
        "label": model,
        "cases": m.get("cases"),
        "n_seen": m.get("n_seen"),
        "n_heldout": m.get("n_heldout"),
        "heldout_classes": m.get("heldout_classes", []),
        "classes": m.get("classes", []),
        "approaches": [a["name"] for a in m.get("approaches", [])] or list(summary),
        "commit": m.get("commit"),
        "dirty": m.get("dirty"),
        "generated_at": m.get("generated_at"),
        "summary": summary,
        "per_class": {
            a: {c: {"n": x["n"], "correct": x["correct"]} for c, x in row.items()}
            for a, row in r.get("per_class", {}).items()
        },
    }


def rel_source(out: Path, src: str | Path) -> str:
    p = Path(src).resolve()
    if p.is_dir():
        p = p / "report.json"
    try:
        return p.relative_to(Path(out).resolve()).as_posix()
    except ValueError:
        return str(p)


def _same_run(a: dict, b: dict) -> bool:
    return (a.get("commit"), a.get("generated_at")) == (b.get("commit"), b.get("generated_at"))


def merge_runs(report: dict, entries: list[dict]) -> dict:
    runs = {(e["model"], e["cases"]): e for e in report.get("live_llm_runs") or []}
    for e in entries:
        old = runs.get((e["model"], e["cases"]))
        if old is not None and "why_wrong" in old and _same_run(old, e):
            e["why_wrong"] = old["why_wrong"]
        runs[(e["model"], e["cases"])] = e
    report["live_llm_runs"] = sorted(runs.values(), key=lambda e: (-(e["cases"] or 0), e["model"]))
    return report


def attach(out: str | Path, sources: list[str], log=print) -> dict:
    out = Path(out)
    rp = out / "report.json"
    if not rp.exists():
        raise FileNotFoundError(f"{rp} not found; run `ic bench` first")
    report = json.loads(rp.read_text())
    entries = [{**run_entry(s), "source": rel_source(out, s)} for s in sources]
    merge_runs(report, entries)
    rpt.write_report(out, report)
    for e in entries:
        log(f"attached {e['model']} ({e['cases']} cases) to {rp}")
    return report


def main(args) -> None:
    attach(args.out, args.attach_llm)
