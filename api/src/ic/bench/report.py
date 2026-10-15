"""Benchmark report: ``report.json`` (everything) and ``report.md`` (the tables the
benchmark page and README show).

Timing fields are the only non-deterministic content: ``strip_timings`` removes them so two
runs with the same seeds can be compared byte for byte."""

from __future__ import annotations

import json
from pathlib import Path
from statistics import mean

TIMING_KEYS = frozenset(
    {"ms", "mean_ms", "latency", "generated_at", "wall_s", "p50_ms", "p95_ms", "timings_s"}
)


def summarize(cases: list[dict], approaches: list[dict]) -> dict:
    """Per-approach accuracy (seen / held-out / overall), mean wall ms, tokens, tool calls."""
    out = {}
    for a in approaches:
        rows = [c for c in cases if c["approach"] == a["name"]]
        seen = [c for c in rows if not c["held_out"]]
        held = [c for c in rows if c["held_out"]]

        def acc(rs: list[dict]) -> float | None:
            return round(sum(r["correct"] for r in rs) / len(rs), 4) if rs else None

        out[a["name"]] = {
            "label": a["label"],
            "n": len(rows),
            "n_seen": len(seen),
            "n_heldout": len(held),
            "correct": sum(r["correct"] for r in rows),
            "accuracy_seen": acc(seen),
            "accuracy_heldout": acc(held),
            "accuracy_overall": acc(rows),
            "mean_ms": round(mean(r["ms"] for r in rows), 1) if rows else None,
            "mean_llm_tokens": round(mean(r["llm_tokens"] for r in rows), 1) if rows else 0,
            "mean_prompt_tokens_est": (
                round(mean(r["prompt_tokens_est"] for r in rows), 1) if rows else 0
            ),
            "mean_tool_calls": round(mean(r["tool_calls"] for r in rows), 2) if rows else 0,
            "truncated": sum(bool(r.get("truncated")) for r in rows),
            "errors": sum(bool(r.get("error")) for r in rows),
        }
    return out


def per_class(cases: list[dict], approaches: list[dict], classes: list[str]) -> dict:
    out: dict[str, dict] = {}
    for a in approaches:
        out[a["name"]] = {}
        for cls in classes:
            rs = [c for c in cases if c["approach"] == a["name"] and c["fault_class"] == cls]
            out[a["name"]][cls] = {
                "n": len(rs),
                "correct": sum(r["correct"] for r in rs),
            }
    return out


def strip_timings(obj):
    """A copy of ``obj`` without timing fields (for reproducibility comparisons)."""
    if isinstance(obj, dict):
        return {k: strip_timings(v) for k, v in obj.items() if k not in TIMING_KEYS}
    if isinstance(obj, list):
        return [strip_timings(v) for v in obj]
    return obj


def _pct(x: float | None) -> str:
    return "n/a" if x is None else f"{x * 100:.0f}%"


def _class_header(cls: str, meta: dict) -> str:
    return f"{cls} (held-out)" if cls in meta["heldout_classes"] else cls


def render_md(r: dict) -> str:
    m = r["meta"]
    lines = [
        "# Incident Commander benchmark",
        "",
        f"{m['cases']} injected faults ({m['n_seen']} training-class, {m['n_heldout']} "
        f"held-out), each in its own isolated copy of the seed world. A prediction is correct "
        "when it names the exact root-cause entity id from the answer key.",
        "",
        "## Accuracy, time and tokens",
        "",
        "| Approach | Accuracy (seen) | Accuracy (held-out) | Overall | Mean ms / case "
        "| LLM tokens / case | Prompt tokens / case (est.) | Tool calls / case |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for s in r["summary"].values():
        lines.append(
            f"| {s['label']} | {_pct(s['accuracy_seen'])} | {_pct(s['accuracy_heldout'])} | "
            f"{_pct(s['accuracy_overall'])} ({s['correct']}/{s['n']}) | {s['mean_ms']:.0f} | "
            f"{s['mean_llm_tokens']:.0f} | {s['mean_prompt_tokens_est']:.0f} | "
            f"{s['mean_tool_calls']:.1f} |"
        )
    classes = m["classes"]
    lines += [
        "",
        "## Correct per fault class",
        "",
        "| Approach | "
        + " | ".join(f"{c}{' (held-out)' if c in m['heldout_classes'] else ''}" for c in classes)
        + " |",
        "|---|" + "---|" * len(classes),
    ]
    for name, row in r["per_class"].items():
        cells = [f"{row[c]['correct']}/{row[c]['n']}" for c in classes]
        lines.append(f"| {r['summary'][name]['label']} | " + " | ".join(cells) + " |")

    lines += ["", "## Per-step latency: FalkorDB vs Postgres", ""]
    lat = r.get("latency")
    if not lat or lat.get("error"):
        lines.append(f"_Not measured: {lat.get('error') if lat else 'skipped'}._")
    else:
        lines += [
            f"Same world ({lat['world']}, {lat['counts']['services']} services, "
            f"{lat['counts']['depends_on']} DEPENDS_ON, {lat['counts']['observed_calls']} "
            f"OBSERVED_CALL, {lat['counts']['postmortems']} postmortems), same host, warm "
            f"cache, {lat['iterations']} timed runs per operation and engine. FalkorDB runs on "
            "a materialized as-of snapshot; Postgres filters valid time inside the query.",
            "",
            "| Operation | FalkorDB p50 ms | FalkorDB p95 ms | Postgres p50 ms | Postgres p95 ms "
            "| Results agree |",
            "|---|---|---|---|---|---|",
        ]
        for op in lat["ops"].values():
            f, p = op["falkordb"], op["postgres"]
            agree = f"{op['agreed']}/{op['checked']}"
            if "exact" in op:
                ex = op["exact"]
                agree += (
                    f" (exact top-k vs brute force: FalkorDB {ex['falkordb']}/{op['checked']}, "
                    f"Postgres {ex['postgres']}/{op['checked']})"
                )
            lines.append(
                f"| {op['label']} | {f['p50_ms']:.2f} | {f['p95_ms']:.2f} | {p['p50_ms']:.2f} | "
                f"{p['p95_ms']:.2f} | {agree} |"
            )
        lines += [
            "",
            "Closure sets and shortest-path lengths are compared input by input; top-k lists "
            "are compared in order. Both vector indexes are approximate (HNSW), so top-k lists "
            "can differ where a neighbour is missed; each engine is also checked against an "
            "exact brute-force ranking.",
        ]

    lines += ["", "## Scale: FalkorDB vs Postgres on a large world", "", *scale_md(r.get("scale"))]
    lines += ["", "## Learning curve", "", *learning_curve_md(r.get("learning_curve"))]
    lines += live_llm_md(r.get("live_llm_runs"))
    lines += why_wrong_md(r.get("live_llm_runs"))
    lines += [
        "",
        "## Notes",
        "",
        *(f"- {n}" for n in m["notes"]),
        "",
        "---",
        "",
        f"Seed {m['seed']} · case seeds {m['case_seeds_digest']} (full list in report.json) · "
        f"commit `{m['commit']}`{' (dirty tree)' if m.get('dirty') else ''} · {m['cases']} cases · "
        f"LLM: {m['llm']}",
    ]
    return "\n".join(lines) + "\n"


def live_llm_md(runs: list[dict] | None) -> list[str]:
    if not runs:
        return []
    out = [
        "",
        "## Live LLM runs",
        "",
        "Separate runs with a real language model behind each approach (the headline table "
        "above is the deterministic run, no LLM). Same fault generator and answer key. "
        "Fallback counts verdicts the deterministic funnel produced after the model failed.",
        "",
        "| Model | Cases | Approach | Overall | Seen | Held-out | LLM tokens / case "
        "| Tool calls / case | Mean ms / case | Fallback |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for run in runs:
        order = [a for a in run.get("approaches") or [] if a in run["summary"]]
        order += [a for a in run["summary"] if a not in order]
        for name in order:
            s = run["summary"][name]
            fallback = "-" if s.get("fallback") is None else f"{s['fallback']}/{s['n']}"
            out.append(
                f"| {run['model']} | {run['cases']} | {name} | "
                f"{_pct(s['accuracy_overall'])} ({s['correct']}/{s['n']}) | "
                f"{_pct(s['accuracy_seen'])} | {_pct(s['accuracy_heldout'])} | "
                f"{s['mean_llm_tokens']:.0f} | {s['mean_tool_calls']:.1f} | "
                f"{(s['mean_ms'] or 0):.0f} | {fallback} |"
            )
    out += [
        "",
        "Runs: "
        + "; ".join(
            f"{r['model']} ({r['cases']} cases, commit `{(r.get('commit') or 'unknown')[:12]}`, "
            f"{r.get('generated_at') or 'n/a'})"
            for r in runs
        )
        + ".",
    ]
    return out


WHY_ORDER = (
    "after_onset",
    "origin",
    "coverage",
    "order",
    "signature",
    "outside_window",
    "not_a_change",
    "rival",
    "verified",
    "unknown_id",
    "inconsistent",
    "cannot_regenerate",
    "no_claim",
)
WHY_EXAMPLES = 3


def why_bucket(row: dict) -> str:
    """The failing clause of a REFUTED row, else the row's verdict."""
    return (
        row["clause"] if row["verdict"] == "REFUTED" and row["clause"] else row["verdict"].lower()
    )


def why_wrong_md(runs: list[dict] | None) -> list[str]:
    """Counts by failing clause per run and approach, plus a few example refutations."""
    runs = [r for r in runs or [] if r.get("why_wrong")]
    if not runs:
        return []
    counts = {
        (r["model"], r["cases"], a): [why_bucket(x) for x in r["why_wrong"] if x["approach"] == a]
        for r in runs
        for a in dict.fromkeys(x["approach"] for x in r["why_wrong"])
    }
    seen = {b for bs in counts.values() for b in bs}
    cols = [b for b in WHY_ORDER if b in seen] + sorted(seen - set(WHY_ORDER))
    out = [
        "",
        "## Why it was wrong",
        "",
        "Every wrong prediction in the live runs above, re-checked with `verify_claim` (the "
        "funnel's own predicate) on its case world regenerated from the seed that case used. "
        "Columns are the first clause the predicted id fails; no_claim means the approach named "
        "no id.",
        "",
        "| Model | Cases | Approach | Wrong | " + " | ".join(cols) + " |",
        "|---|---|---|---|" + "---|" * len(cols),
    ]
    for (model, cases, a), bs in counts.items():
        cells = [str(bs.count(c)) if c in bs else "-" for c in cols]
        out.append(f"| {model} | {cases} | {a} | {len(bs)} | " + " | ".join(cells) + " |")
    out += [
        "",
        "| Model | Case | Fault class | Approach | Predicted | Verdict | Why |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in runs:
        refuted = [x for x in r["why_wrong"] if x["verdict"] == "REFUTED"]
        for x in refuted[:WHY_EXAMPLES]:
            out.append(
                f"| {r['model']} | {x['case']} | {x['fault_class']} | {x['approach']} | "
                f"`{x['predicted']}` | REFUTED · {x['clause']} | {x['detail']} |"
            )
    out += [
        "",
        "Every row with its deciding query is in report.json (`live_llm_runs[].why_wrong`).",
    ]
    return out


def scale_md(sc: dict | None) -> list[str]:
    """The scale section (``ic bench --scale N``): deeper traversals on a large world."""
    if not sc:
        return ["_Not yet measured (run `ic bench --scale N`)._"]
    if sc.get("error"):
        return [f"_Not measured ({sc['world']}): {sc['error']}._"]
    c = sc["counts"]
    t = sc.get("timings_s") or {}
    out = [
        f"`{sc['world']}`: {c['services']} services, {c['depends_on']} DEPENDS_ON, "
        f"{c['observed_calls']} OBSERVED_CALL, {c['changes']} changes "
        f"({c.get('impacts_service_edges', 'n/a')} Service->Service IMPACTS edges in the "
        f"as-of snapshot). {sc['iterations']} timed runs per operation and engine.",
        "",
        "| Operation | FalkorDB p50 ms | FalkorDB p95 ms | Postgres p50 ms | Postgres p95 ms "
        "| Mean result | Results agree |",
        "|---|---|---|---|---|---|---|",
    ]
    for key, op in sc["ops"].items():
        f, p = op["falkordb"], op["postgres"]
        unit = "hops" if key == "path" else "services"
        size = "n/a" if op.get("mean_size") is None else f"{op['mean_size']} {unit}"
        ref = op.get("reference")
        agree = f"{op['agreed']}/{op['checked']}"
        if ref:
            agree += f" (vs BFS: FalkorDB {ref['falkordb']}, Postgres {ref['postgres']})"
        out.append(
            f"| {op['label']} | {f['p50_ms']:.2f} | {f['p95_ms']:.2f} | {p['p50_ms']:.2f} | "
            f"{p['p95_ms']:.2f} | {size} | {agree} |"
        )
    if t:
        out += [
            "",
            "Setup (not part of the timings): "
            + ", ".join(f"{k.replace('_', ' ')} {v} s" for k, v in t.items())
            + ".",
        ]
    out += ["", *(f"- {n}" for n in sc.get("notes") or [])]
    out += ["", f"Seed {sc['seed']} · commit `{sc.get('commit')}`."]
    return out


def learning_curve_md(lc: dict | None) -> list[str]:
    """The learning-curve section: tool calls per incident in one session with memory."""
    if not lc or not lc.get("incidents"):
        return ["_Not yet measured (run `ic learn`)._"]
    m, rows, s = lc["meta"], lc["incidents"], lc.get("summary") or {}
    c = m["coupling"]
    out = [
        f"{m['runs']} undeclared-coupling incidents investigated in sequence in ONE session "
        f"world, memory written back after each resolution (KTD15: separate from the "
        f"accuracy runs, which reset memory per case). Incident 1 teaches "
        f"`{c['caller']} -> {c['callee']}`; every later fault is rooted on `{c['callee']}` "
        "and its symptoms travel through that coupling to a different alert service where "
        f"possible. Triage: {m['llm']}.",
        "",
        "| # | Incident | Alert service | Verdict | Tool calls | LLM steps | Wall ms "
        "| Recall seeded | Learned edge after |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for i, r in enumerate(rows, 1):
        edge = next(
            (
                e
                for e in r.get("learned") or []
                if e["caller"] == c["caller"] and e["callee"] == c["callee"]
            ),
            None,
        )
        after = f"{edge['action']} ({edge['confidence']:.2f})" if edge else "-"
        if not edge and r.get("edge_confidence") is not None:
            after = f"untouched ({r['edge_confidence']:.2f})"
        if r.get("phase") == "forget":
            after += " · forget"
        verdict = f"{r['verdict']}{'' if r['correct'] else ' (wrong)'}"
        out.append(
            f"| {i} | {r['incident_id']} | `{r['alert_service']}` | {verdict} | "
            f"{r['tool_calls']} | {r['steps']} | {r['ms']:.0f} | "
            f"{'yes' if r['memory_seeded'] else 'no'} | {after} |"
        )
    if s.get("later_mean_tool_calls") is not None:
        out += [
            "",
            f"Tool calls: incident 1 = {s['first_tool_calls']}, incident 2 = "
            f"{s['second_tool_calls']}, mean of incidents 2-{len(rows)} = "
            f"{s['later_mean_tool_calls']} ({s.get('reduction_pct')}% fewer). "
            f"{s['correct']}/{s['n']} correct.",
        ]
    fg = m.get("forget")
    if fg:
        series = ", ".join(
            f"{r['edge_confidence']:.3f}" for r in rows if r.get("edge_confidence") is not None
        )
        check = next((r for r in rows if r.get("check")), None)
        out += [
            "",
            f"Forget phase: after {fg['change']['id']} ({fg['change']['summary']}), "
            f"{fg['contradictions']} incident(s) contradicted the learned edge, each halving "
            f"its confidence; recall seeds only from edges at or above {fg['threshold']}. "
            f"Edge confidence per incident: {series}.",
        ]
        if check:
            out[-1] += (
                f" {check['incident_id']} ran with the edge under the threshold: recall "
                f"{'still' if check.get('edge_seeded') else 'no longer'} seeded from it "
                f"({check['tool_calls']} tool calls)."
            )
    out += ["", m["method"], "", f"Seeds {m['seeds']} · commit `{m['commit']}`."]
    return out


def write_report(out_dir: str | Path, report: dict) -> tuple[Path, Path]:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    jp, mp = out / "report.json", out / "report.md"
    jp.write_text(json.dumps(report, indent=1, sort_keys=True) + "\n")
    mp.write_text(render_md(report))
    return jp, mp
