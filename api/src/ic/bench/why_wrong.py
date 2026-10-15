"""Why every wrong live-LLM answer was wrong (R3, KTD4). No LLM calls.

``ic bench --why-wrong`` reads each attached live run's own report (``live_llm_runs[].source``,
else the ``*.json`` report under the output directory with the same model and case count),
joins its wrong predictions to ``case_list``, regenerates each needed case world once from the
seed that case actually used (``setup_case``) and runs ``verify_claim`` on the known as-of
snapshot, the view verdicts use. The compact rows land in ``live_llm_runs[].why_wrong``.

Row verdicts: ``REFUTED`` / ``RIVAL`` from ``verify_claim``; ``no_claim`` when the approach
named no id (nothing is regenerated); ``cannot_regenerate`` when the case has no stored seed
or the regenerated world does not match the stored case (root id, alert time, alert service
and incident id: root ids are sequential, so the root alone does not pin the world);
``unknown_id`` when the id is not in the snapshot; ``inconsistent`` when ``verify_claim``
VERIFIES a prediction the report graded wrong (the regenerated world disagrees with the
grade, so it is not published as an explanation). Root ids are never copied into the rows."""

from __future__ import annotations

import asyncio
import json
import secrets
import tempfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from ic.bench import live_llm
from ic.bench import report as rpt
from ic.bench.common import CaseView
from ic.bench.runner import CasePlan, _drop_prefix, setup_case
from ic.graph import client
from ic.store import Store
from ic.tools.claims import verify_claim
from ic.tools.result import ToolError
from ic.world.faults import InjectionError


def _row(x: dict, verdict: str, clause=None, detail=None, query_text=None) -> dict:
    return {
        "case": x["case"],
        "fault_class": x["fault_class"],
        "held_out": x["held_out"],
        "approach": x["approach"],
        "predicted": x["predicted"],
        "verdict": verdict,
        "clause": clause,
        "detail": detail,
        "query_text": query_text,
    }


@asynccontextmanager
async def regenerated(case: dict, store: Store) -> AsyncIterator[tuple[CaseView, str]]:
    """The case world rebuilt from its used seed; every graph it made is dropped on exit."""
    run = f"w{secrets.token_hex(4)}"
    plan = CasePlan(
        int(case["case"][1:]), case["case"], case["fault_class"], case["held_out"], case["rng_seed"]
    )
    view = None
    try:
        view, root, _ = await setup_case(run, plan, store)
        yield view, root
    finally:
        await _drop_prefix(f"b:{run}:")
        if view is not None:
            await _drop_prefix(f"s:{view.sid}:")


# (CaseView attribute, case_list key): the seed-dependent facts that pin the world.
FINGERPRINT = (
    ("alert_time", "alert_time"),
    ("alert_service", "alert_service"),
    ("incident_id", "incident_id"),
)


def _mismatch(view: CaseView, root: str, stored: dict) -> str | None:
    """Which stored fact the regenerated case disagrees with, if any."""
    if root != stored.get("root_id"):
        return "the regenerated incident has a different root cause"
    for attr, key in FINGERPRINT:
        if getattr(view, attr) != stored.get(key):
            return f"the regenerated case has a different {key} than the recorded run"
    return None


async def _check(view: CaseView, x: dict) -> dict:
    try:
        r = (await verify_claim(view.snapshot_known, view.incident_id, x["predicted"])).result
    except ToolError as e:
        return _row(x, "unknown_id", detail=str(e))
    if r["verdict"] == "VERIFIED":
        return _row(
            x, "inconsistent", detail="the regenerated world disagrees with the grade (VERIFIED)"
        )
    return _row(x, r["verdict"], r["clause"], r["detail"], r["query_text"])


async def explain_runs(reports: list[dict], log=print) -> list[list[dict]]:
    """Rows for every wrong prediction of each report, in (case, approach) order. Cases shared
    by several runs (same seed and class) are regenerated once."""
    out: list[dict[int, dict]] = [{} for _ in reports]
    # (fault_class, used seed) -> case meta and the (report, row index, prediction) to check
    todo: dict[tuple[str, int], tuple[dict, list[tuple[int, int, dict]]]] = {}
    for ri, rep in enumerate(reports):
        cases = {c["case"]: c for c in rep.get("case_list") or []}
        wrong = [x for x in rep.get("results") or [] if not x.get("correct")]
        for i, x in enumerate(sorted(wrong, key=lambda x: (x["case"], x["approach"]))):
            case = cases.get(x["case"])
            if x.get("predicted") is None:
                out[ri][i] = _row(x, "no_claim")
            elif case is None or case.get("rng_seed") is None:
                out[ri][i] = _row(x, "cannot_regenerate", detail="no stored seed for this case")
            else:
                key = (case["fault_class"], case["rng_seed"])
                todo.setdefault(key, (case, []))[1].append((ri, i, x))
    with tempfile.TemporaryDirectory(prefix="ic-why-") as tmp:
        store = Store(Path(tmp) / "why.sqlite3")
        for case, checks in todo.values():
            try:
                async with regenerated(case, store) as (view, root):
                    for ri, i, x in checks:
                        stored = next(c for c in reports[ri]["case_list"] if c["case"] == x["case"])
                        why = _mismatch(view, root, stored)
                        if why is not None:
                            out[ri][i] = _row(x, "cannot_regenerate", detail=why)
                        else:
                            out[ri][i] = await _check(view, x)
            except InjectionError as e:
                for ri, i, x in checks:
                    out[ri][i] = _row(x, "cannot_regenerate", detail=f"injection failed: {e}")
            log(f"{case['case']} {case['fault_class']:<20} checked {len(checks)} prediction(s)")
    return [[rows[i] for i in sorted(rows)] for rows in out]


def _source(out: Path, entry: dict) -> Path | None:
    """The run's own report: its recorded source, else a report under ``out`` that matches."""
    if entry.get("source"):
        p = out / entry["source"]
        return p if p.exists() else None
    for p in sorted(out.rglob("*.json")):
        if p.parent == out:
            continue  # the main report, learning curve, scale
        try:
            meta = json.loads(p.read_text()).get("meta") or {}
        except (ValueError, AttributeError):
            continue
        if meta.get("llm") == entry["model"] and meta.get("cases") == entry["cases"]:
            return p
    return None


async def attach(out: str | Path, log=print) -> dict:
    """Compute ``why_wrong`` for every attached live run and rewrite the report."""
    out = Path(out)
    rp = out / "report.json"
    report = json.loads(rp.read_text())
    entries, reports = [], []
    for e in report.get("live_llm_runs") or []:
        src = _source(out, e)
        if src is None:
            log(f"{e['model']} ({e['cases']} cases): no per-run report found, skipped")
            continue
        r = json.loads(src.read_text())
        e["source"] = live_llm.rel_source(out, src)
        entries.append(e)
        reports.append(r)
    rows = await explain_runs(reports, log=log)
    for e, rs in zip(entries, rows, strict=True):
        e["why_wrong"] = rs
        log(f"{e['model']} ({e['cases']} cases): {len(rs)} wrong prediction(s) explained")
    rpt.write_report(out, report)
    return report


def main(args) -> None:
    try:
        asyncio.run(attach(args.out))
    finally:
        client.reset_client()
