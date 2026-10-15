"""``ic bench``: inject many faults, run every approach on each, score against the answer key.

Per case: copy ``seed:world`` into ``b:{run}:{case}:world``, inject a fault (classes cycle
through the three training classes and the two held-out ones in a seeded order), build the
``true``/``known`` as-of views at alert time, run each approach, and score it: correct means
the exact root entity id from the answer key. Then every ``b:{run}:*`` graph (and the case's
``s:{sid}:*`` ledger scope) is deleted.

Approaches:
  graph         the triage agent (``run_triage``). With no LLM key this is the deterministic
                graph fallback: the KTD23 funnel and proof chain over IMPACTS.
  vector_rag    similarity search over change and postmortem chunks (no graph traversal)
  full_context  declared topology + change log + symptoms in one prompt (no tools)

Fairness (KTD15): memory is reset per case (fresh world and ledger), every approach sees the
same snapshot and candidate window, the LLM (when there is one) is the same client for all.
The commit hash and every seed go into the report.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import random
import secrets
import subprocess
import tempfile
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from ic.agent.llm import LLMClient, LLMConfigError, make_llm
from ic.agent.triage import run_triage
from ic.bench import report as rpt
from ic.bench.baselines.full_context import HEURISTIC, full_context
from ic.bench.baselines.vector_rag import vector_rag
from ic.bench.common import Approach, BenchContext, CaseView, Prediction
from ic.bench.scale import existing_scale
from ic.config import get_settings
from ic.graph import client, naming
from ic.graph.schema import WORLD_NOW
from ic.graph.snapshots import build_view
from ic.store import Store
from ic.tools.registry import ToolContext
from ic.world.faults import (
    HELDOUT_CLASSES,
    TRAINING_CLASSES,
    InjectionError,
    inject_fault,
)

DEFAULT_OUT = Path(__file__).resolve().parents[3] / "bench_results"
CLASS_CYCLE = (*TRAINING_CLASSES, *HELDOUT_CLASSES)
DEFAULT_APPROACHES = ("graph", "vector_rag", "full_context")
INJECT_ATTEMPTS = 4


async def graph_agent(case: CaseView, bctx: BenchContext) -> Prediction:
    """The triage agent on the case's snapshots. The ledger lives in the case's session
    scope; its incident is bound to a session copy of the benchmark world."""
    ctx = ToolContext(
        sid=case.sid,
        iid=case.incident_id,
        run=case.run,
        world_graph=naming.session_world(case.sid),
        snapshot_true=case.snapshot_true,
        snapshot_known=case.snapshot_known,
        ledger_graph=naming.ledger(case.sid),
        trusted_t=case.alert_time,
        trusted_world=naming.session_world(case.sid),
    )
    tokens = 0

    def on_usage(model: str, prompt: int, completion: int) -> None:
        nonlocal tokens
        tokens += prompt + completion

    out = await run_triage(
        ctx, case.incident_id, llm=bctx.llm, emit=None, store=bctx.store, on_usage=on_usage
    )
    predicted = out.suspect_id if out.verdict == "DIAGNOSED" else None  # INCONCLUSIVE: no claim
    return Prediction(
        predicted,
        llm_tokens=tokens,
        tool_calls=out.tool_calls,
        verdict=out.verdict,
        notes={
            "degraded": out.degraded,
            "degraded_reason": out.degraded_reason,
            "rivals": out.rival_ids,
            "steps": out.steps,
        },
    )


BUILTIN: dict[str, Approach] = {
    "graph": graph_agent,
    "vector_rag": vector_rag,
    "full_context": full_context,
}


def label(name: str, llm: LLMClient | None) -> str:
    if llm is not None:
        return f"{name} (llm: {getattr(llm, 'model', '?')})"
    return {
        "graph": "graph (deterministic)",
        "vector_rag": "vector_rag (hashing embedder, no LLM)",
        "full_context": "full_context (heuristic, no LLM)",
    }.get(name, name)


@dataclass(frozen=True)
class CasePlan:
    index: int
    case_id: str
    fault_class: str
    held_out: bool
    rng_seed: int


def plan_cases(seed: int, n: int) -> tuple[list[str], list[CasePlan]]:
    """Seeded class order, cycled; one injection seed per case."""
    rng = random.Random(f"bench:{seed}")
    order = list(CLASS_CYCLE)
    rng.shuffle(order)
    plans = []
    for i in range(n):
        cls = order[i % len(order)]
        plans.append(CasePlan(i, f"c{i:03d}", cls, cls in HELDOUT_CLASSES, rng.randrange(1, 10**6)))
    return order, plans


def git_state() -> tuple[str, bool]:
    root = Path(__file__).resolve().parents[3]
    try:
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True, check=True
        ).stdout.strip()
        dirty = bool(
            subprocess.run(
                ["git", "status", "--porcelain", "--untracked-files=no"],
                cwd=root,
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()
        )
        return head, dirty
    except (OSError, subprocess.CalledProcessError):
        return "unknown", False


async def _drop_prefix(prefix: str) -> None:
    db = client.get_db()
    for name in await db.list_graphs():
        if name.startswith(prefix):
            await db.select_graph(name).delete()


async def setup_case(run: str, plan: CasePlan, store: Store) -> tuple[CaseView, str, int]:
    """World copy, injection (re-seeded on InjectionError), views. Returns (view, root, seed)."""
    world = naming.bench(run, plan.case_id, "world")
    sid = naming.new_session_id()
    last: Exception | None = None
    for attempt in range(INJECT_ATTEMPTS):
        rng_seed = plan.rng_seed + 7919 * attempt
        await client.copy_graph(naming.seed_world(), world)
        try:
            f = await inject_fault(
                world, session_id=sid, fault_class=plan.fault_class, rng_seed=rng_seed, store=store
            )
            break
        except InjectionError as e:
            last = e
    else:
        raise InjectionError(f"{plan.case_id}: {last}")
    answer = store.get_answer(f.incident_id, sid)
    assert answer is not None
    true_g = naming.bench(run, plan.case_id, "asof-true")
    known_g = naming.bench(run, plan.case_id, "asof-known")
    await asyncio.gather(
        build_view(world, true_g, f.alert_time, "true"),
        build_view(world, known_g, f.alert_time, "known"),
        client.copy_graph(world, naming.session_world(sid)),  # ledger binding for the agent
    )
    view = CaseView(
        run=run,
        case_id=plan.case_id,
        sid=sid,
        incident_id=f.incident_id,
        alert_time=f.alert_time,
        alert_service=f.alert_service_id,
        world=world,
        snapshot_true=true_g,
        snapshot_known=known_g,
    )
    return view, answer["root_id"], rng_seed


def resolve_llm(mode: str) -> LLMClient | None:
    """``none``: deterministic/heuristic modes. ``auto``: the configured provider if a key is
    available, otherwise none."""
    if mode == "none":
        return None
    try:
        return make_llm(get_settings())
    except LLMConfigError:
        if mode == "llm":
            raise
        return None


async def run_bench(
    *,
    seed: int = 42,
    cases: int = 50,
    approaches: list[str] | dict[str, Approach] = DEFAULT_APPROACHES,
    out: str | Path | None = DEFAULT_OUT,
    llm: LLMClient | None = None,
    latency: bool = True,
    latency_iters: int = 50,
    store: Store | None = None,
    context_limit: int = 128_000,
    log=print,
) -> dict:
    """Run the benchmark and (when ``out`` is set) write report.json and report.md."""
    t_start = time.perf_counter()
    if isinstance(approaches, dict):
        fns = dict(approaches)
    else:
        unknown = [a for a in approaches if a not in BUILTIN]
        if unknown:
            raise ValueError(f"unknown approaches {unknown}; known: {sorted(BUILTIN)}")
        fns = {a: BUILTIN[a] for a in approaches}
    run = f"r{seed}-{secrets.token_hex(4)}"
    tmp = None
    if store is None:
        tmp = tempfile.TemporaryDirectory(prefix="ic-bench-")
        store = Store(Path(tmp.name) / "bench.sqlite3")
    bctx = BenchContext(llm=llm, store=store, context_limit=context_limit)
    order, plans = plan_cases(seed, cases)
    commit, dirty = git_state()
    rows: list[dict] = []
    case_meta: list[dict] = []
    failures: list[dict] = []
    try:
        for plan in plans:
            try:
                view, root_id, used_seed = await setup_case(run, plan, store)
            except InjectionError as e:
                failures.append(
                    {"case": plan.case_id, "fault_class": plan.fault_class, "error": str(e)}
                )
                log(f"{plan.case_id} {plan.fault_class:<20} injection failed: {e}")
                await _drop_prefix(f"b:{run}:{plan.case_id}:")
                continue
            case_meta.append(
                {
                    "case": plan.case_id,
                    "fault_class": plan.fault_class,
                    "held_out": plan.held_out,
                    "rng_seed": used_seed,
                    "incident_id": view.incident_id,
                    "alert_service": view.alert_service,
                    "alert_time": view.alert_time,
                    "root_id": root_id,
                }
            )
            try:
                marks = []
                for name, fn in fns.items():
                    t0 = time.perf_counter()
                    error = None
                    try:
                        pred = await fn(view, bctx)
                    except Exception as e:  # an approach failing is a wrong answer, not a crash
                        pred, error = Prediction(None), f"{type(e).__name__}: {e}"
                    ms = round((time.perf_counter() - t0) * 1000, 1)
                    correct = pred.predicted is not None and pred.predicted == root_id
                    marks.append("+" if correct else "-")
                    rows.append(
                        {
                            "case": plan.case_id,
                            "fault_class": plan.fault_class,
                            "held_out": plan.held_out,
                            "approach": name,
                            "predicted": pred.predicted,
                            "root_id": root_id,
                            "correct": correct,
                            "verdict": pred.verdict,
                            "ms": ms,
                            "llm_tokens": pred.llm_tokens,
                            "prompt_tokens_est": pred.prompt_tokens_est,
                            "tool_calls": pred.tool_calls,
                            "truncated": pred.truncated,
                            "error": error,
                            "notes": pred.notes,
                        }
                    )
                log(
                    f"{plan.case_id} {plan.fault_class:<20} root={root_id} "
                    + " ".join(f"{n}{m}" for n, m in zip(fns, marks, strict=True))
                )
            finally:
                await _drop_prefix(f"b:{run}:{plan.case_id}:")
                await _drop_prefix(f"s:{view.sid}:")

        lat = None
        if latency:
            lat = await measure_latency(run, seed, latency_iters, log=log)
    finally:
        await _drop_prefix(f"b:{run}:")
        if tmp is not None:
            tmp.cleanup()

    approach_meta = [{"name": n, "label": label(n, llm)} for n in fns]
    seeds = [c["rng_seed"] for c in case_meta]
    notes = []
    if llm is None:
        notes.append(
            "No LLM key was configured: graph = deterministic graph fallback (KTD23 funnel + "
            "proof chain over IMPACTS, verdict gate unchanged); vector_rag = nearest candidate "
            "chunk by HashingEmbedder cosine similarity; full_context = " + HEURISTIC + ". "
            "LLM-backed modes are wired (ic bench --llm auto) and need a key."
        )
    notes += [
        "Correct = exact root entity id (change or expiry) from the answer key; an "
        "INCONCLUSIVE graph verdict makes no claim and counts as not correct.",
        "Held-out classes (expiry, undeclared_coupling) were written after the triage prompts "
        "and tool descriptions, which do not change in this unit and never name them as fault "
        "classes (tested; Expiry as a graph label and the observed_calls tool predate them).",
        "Every approach reads the same as-of snapshot (known view at alert time) and the same "
        "candidate window [earliest onset - 120 min, alert time].",
        "full_context serializes the declared service catalog (DEPENDS_ON only, no "
        "OBSERVED_CALL); prompt tokens are estimated as len(text)/4 against a "
        f"{context_limit:,}-token limit.",
        "Mean ms is wall time per case for the approach alone (world copy, injection and "
        "snapshot builds excluded).",
    ]
    if failures:
        notes.append(f"{len(failures)} case(s) skipped: no valid injection found.")
    held = sum(c["held_out"] for c in case_meta)
    report = {
        "meta": {
            "seed": seed,
            "cases": len(case_meta),
            "requested_cases": cases,
            "n_seen": len(case_meta) - held,
            "n_heldout": held,
            "commit": commit,
            "dirty": dirty,
            "classes": list(CLASS_CYCLE),
            "class_order": order,
            "training_classes": list(TRAINING_CLASSES),
            "heldout_classes": list(HELDOUT_CLASSES),
            "case_seeds": seeds,
            "case_seeds_digest": hashlib.sha256(repr(seeds).encode()).hexdigest()[:12],
            "approaches": approach_meta,
            "llm": "none (deterministic/heuristic modes)"
            if llm is None
            else getattr(llm, "model", "?"),
            "context_limit": context_limit,
            "world": naming.seed_world(),
            "notes": notes,
            "generated_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "wall_s": round(time.perf_counter() - t_start, 1),
        },
        "summary": rpt.summarize(rows, approach_meta),
        "per_class": rpt.per_class(rows, approach_meta, list(CLASS_CYCLE)),
        "case_list": case_meta,
        "results": rows,
        "failures": failures,
        "latency": lat,
        "learning_curve": _existing_learning_curve(out),
        "scale": existing_scale(out),
    }
    if out is not None:
        jp, mp = rpt.write_report(out, report)
        log(f"wrote {jp} and {mp}")
    return report


def _existing_learning_curve(out: str | Path | None) -> dict | None:
    """Keep the separately-run learning curve (``ic learn``) when the report is rewritten."""
    if out is None:
        return None
    p = Path(out) / "learning_curve.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except ValueError:
        return None


async def measure_latency(run: str, seed: int, iterations: int, log=print) -> dict:
    """FalkorDB (as-of snapshot of seed:world at WORLD_NOW) vs the Postgres twin."""
    from ic.bench import pg_twin

    snap = naming.bench(run, "latency", "asof-true")
    schema = f"twin_{run.replace('-', '_')}"
    try:
        await build_view(naming.seed_world(), snap, WORLD_NOW, "true")
        rows = await pg_twin.read_world(naming.seed_world())
        try:
            twin = pg_twin.PgTwin(get_settings().postgres_dsn, schema)
        except Exception as e:  # no Postgres: report it instead of failing the benchmark
            log(f"latency skipped: {e}")
            return {"error": f"Postgres unavailable: {type(e).__name__}"}
        with twin:
            twin.load(rows)
            res = await pg_twin.compare(snap, twin, WORLD_NOW, iterations=iterations, seed=seed)
            res["counts"] = twin.counts()
        res["world"] = naming.seed_world()
        for op in res["ops"].values():
            log(
                f"latency {op['label']}: falkordb p50 {op['falkordb']['p50_ms']} ms, "
                f"postgres p50 {op['postgres']['p50_ms']} ms, agree {op['agreed']}/{op['checked']}"
            )
        return res
    finally:
        await client.delete_graph(snap)


def main(args) -> None:
    approaches = [a.strip() for a in args.approaches.split(",") if a.strip()]
    llm = resolve_llm(args.llm)
    try:
        asyncio.run(
            run_bench(
                seed=args.seed,
                cases=args.cases,
                approaches=approaches,
                out=args.out,
                llm=llm,
                latency=not args.no_latency,
                latency_iters=args.latency_iters,
            )
        )
    finally:
        client.reset_client()
