"""Benchmark runner (U15): one case per class end to end, scoring, LLM wiring with a stub
client, held-out names kept out of the frozen prompts, reproducible reports."""

from __future__ import annotations

import json

import pytest

from ic.agent import prompts
from ic.agent.llm import ScriptedLLM, reply
from ic.bench import report as rpt
from ic.bench import runner
from ic.bench.baselines.full_context import full_context
from ic.bench.baselines.vector_rag import vector_rag
from ic.bench.common import BenchContext, CaseView, Prediction, pick_id
from ic.graph import client, naming
from ic.graph.schema import Label
from ic.store import Store
from ic.tools.registry import openai_tool_schemas
from ic.world.faults import HELDOUT_CLASSES, TRAINING_CLASSES

SEED = 42
N = 5


async def stub(case: CaseView, bctx: BenchContext) -> Prediction:
    """Right for even cases, a wrong id for odd ones, no answer for the last."""
    i = int(case.case_id[1:])
    if i == N - 1:
        return Prediction(None)
    if i % 2 == 0:
        return Prediction(bctx.store.get_answer(case.incident_id, case.sid)["root_id"])
    return Prediction("chg-00001")


async def _leftovers(prefix: str = f"b:r{SEED}-") -> list[str]:
    return [g for g in await client.get_db().list_graphs() if g.startswith(prefix)]


@pytest.fixture(scope="module")
async def bench_runs(tmp_path_factory):
    """Two runs with the same seed (5 cases: one per class): the builtin approaches plus the
    stub. Shared by the tests below so the suite pays for it once."""
    client.reset_client()
    # Other `ic bench --seed 42` processes may share this FalkorDB: only this fixture's own
    # run ids count as leaks.
    runs: list[str] = []
    real_token_hex = runner.secrets.token_hex

    def recording_token_hex(n):
        runs.append(real_token_hex(n))
        return runs[-1]

    mp = pytest.MonkeyPatch()
    mp.setattr(runner.secrets, "token_hex", recording_token_hex)
    out_a = tmp_path_factory.mktemp("bench_a")
    out_b = tmp_path_factory.mktemp("bench_b")
    approaches = {**runner.BUILTIN, "stub": stub}
    quiet = lambda *_: None  # noqa: E731
    a = await runner.run_bench(
        seed=SEED, cases=N, approaches=approaches, out=out_a, latency=False, log=quiet
    )
    b = await runner.run_bench(
        seed=SEED, cases=N, approaches=approaches, out=out_b, latency=False, log=quiet
    )
    mp.undo()
    leaked = [g for r in runs for g in await _leftovers(f"b:r{SEED}-{r}:")]
    return {"a": a, "b": b, "out_a": out_a, "out_b": out_b, "leaked": leaked}


def test_plan_cycles_every_class_with_seeded_order():
    order, plans = runner.plan_cases(SEED, 10)
    assert sorted(order) == sorted([*TRAINING_CLASSES, *HELDOUT_CLASSES])
    assert [p.fault_class for p in plans[:5]] == order
    assert [p.fault_class for p in plans[5:]] == order
    assert all(p.held_out == (p.fault_class in HELDOUT_CLASSES) for p in plans)
    assert runner.plan_cases(SEED, 10) == (order, plans)
    assert runner.plan_cases(SEED + 1, 10) != (order, plans)


async def test_runner_completes_one_case_per_class(bench_runs):
    r = bench_runs["a"]
    assert r["meta"]["cases"] == N
    assert sorted(c["fault_class"] for c in r["case_list"]) == sorted(runner.CLASS_CYCLE)
    assert r["meta"]["n_heldout"] == 2 and r["meta"]["n_seen"] == 3
    assert len(r["results"]) == N * 4
    assert not bench_runs["leaked"], bench_runs["leaked"]  # every b:{run}:* graph is gone
    # the deterministic graph agent solves every class, held-out included
    graph = [x for x in r["results"] if x["approach"] == "graph"]
    assert all(x["correct"] for x in graph), [(x["fault_class"], x["predicted"]) for x in graph]
    assert all(x["verdict"] == "DIAGNOSED" and x["llm_tokens"] == 0 for x in graph)
    assert r["summary"]["graph"]["label"] == "graph (deterministic)"
    fc = [x for x in r["results"] if x["approach"] == "full_context"]
    assert all(x["prompt_tokens_est"] > 1000 and not x["truncated"] for x in fc)


async def test_scoring_marks_correct_and_incorrect(bench_runs):
    r = bench_runs["a"]
    rows = {x["case"]: x for x in r["results"] if x["approach"] == "stub"}
    for case, x in rows.items():
        i = int(case[1:])
        if i == N - 1:
            assert x["predicted"] is None and not x["correct"]
        elif i % 2 == 0:
            assert x["predicted"] == x["root_id"] and x["correct"]
        else:
            assert x["predicted"] == "chg-00001" and not x["correct"]
    s = r["summary"]["stub"]
    assert s["correct"] == 2 and s["n"] == N
    assert s["accuracy_overall"] == pytest.approx(0.4)


async def test_report_files_written_and_reproducible(bench_runs):
    for key in ("out_a", "out_b"):
        assert (bench_runs[key] / "report.json").exists()
        md = (bench_runs[key] / "report.md").read_text()
        assert "| Approach | Accuracy (seen) | Accuracy (held-out) |" in md
        assert "## Learning curve" in md
        assert bench_runs["a"]["meta"]["commit"] in md
    ja = json.loads((bench_runs["out_a"] / "report.json").read_text())
    jb = json.loads((bench_runs["out_b"] / "report.json").read_text())
    assert rpt.strip_timings(ja) == rpt.strip_timings(jb)
    assert ja["meta"]["case_seeds"] == jb["meta"]["case_seeds"]
    assert ja["learning_curve"] is None


def test_heldout_class_names_never_appear_in_prompts_or_tool_descriptions():
    """KTD15: the frozen prompts and tool schemas must not name the held-out classes. The
    word 'expiry' is graph vocabulary (the Expiry label predates the freeze), so only the
    class identifiers themselves, and phrases naming them as fault classes, are banned."""
    brief = prompts.incident_brief(
        "inc-001", alert_service="svc-x@us-east", opened_at="2026-10-01T00:00:00Z", title="t"
    )
    with open(prompts.__file__) as f:
        source = f.read()
    blobs = [
        prompts.SYSTEM,
        prompts.VERDICT_DESCRIPTION,
        json.dumps(prompts.SUBMIT_VERDICT_SCHEMA),
        json.dumps(openai_tool_schemas()),
        brief,
        source,
    ]
    text = "\n".join(blobs).lower()
    labels = {str(lbl).lower() for lbl in Label}
    for cls in HELDOUT_CLASSES:
        if cls in labels:
            for phrase in (f"{cls} fault", f"{cls} class", f'"{cls}"', f"'{cls}'"):
                assert phrase not in text, phrase
        else:
            assert cls not in text, cls
            assert cls.replace("_", " ") not in text, cls
    for word in ("held-out", "held_out", "heldout", "fault_class", "fault class"):
        assert word not in text, word
    assert "undeclared_coupling" not in text


def test_pick_id():
    assert pick_id("It was chg-00012, not exp-0003.", ["exp-0003"]) == "exp-0003"
    assert pick_id("It was chg-00012.", ["exp-0003"]) == "chg-00012"
    assert pick_id("no idea", ["exp-0003"]) is None
    assert pick_id(None, []) is None


@pytest.fixture
async def one_case(tmp_path):
    client.reset_client()
    store = Store(tmp_path / "ic.sqlite3")
    run = "t" + naming.new_session_id()[:8]
    _, plans = runner.plan_cases(SEED, 5)
    plan = next(p for p in plans if p.fault_class == "undeclared_coupling")
    view, root, _ = await runner.setup_case(run, plan, store)
    yield view, root, store
    await runner._drop_prefix(f"b:{run}:")
    await runner._drop_prefix(f"s:{view.sid}:")


async def test_llm_modes_use_the_client_and_count_tokens(one_case):
    view, root, store = one_case

    def answer(messages, tools):
        assert tools == []  # baselines get no tools
        assert "chg-" in messages[1]["content"] or "exp-" in messages[1]["content"]
        return reply(content=f"The cause is {root}.", tokens=(1200, 8))

    for fn in (vector_rag, full_context):
        llm = ScriptedLLM([answer])
        p = await fn(view, BenchContext(llm=llm, store=store))
        assert len(llm.calls) == 1
        assert p.llm_tokens == 1208
        assert p.prompt_tokens_est > 0
        assert p.predicted == root

    # graph (llm): a failing client degrades to the deterministic funnel, still scored
    llm = ScriptedLLM([RuntimeError("down"), RuntimeError("down")])
    p = await runner.graph_agent(view, BenchContext(llm=llm, store=store))
    assert p.predicted == root and p.notes["degraded"]
    assert runner.label("graph", llm) == "graph (llm: scripted)"
    assert runner.label("graph", None) == "graph (deterministic)"


async def test_full_context_reports_truncation(one_case):
    view, root, store = one_case
    p = await full_context(view, BenchContext(llm=None, store=store, context_limit=1000))
    assert p.truncated and p.prompt_tokens_est > 1000 and p.llm_tokens == 0
    llm = ScriptedLLM([reply(content=root, tokens=(900, 2))])
    p = await full_context(view, BenchContext(llm=llm, store=store, context_limit=4000))
    assert p.truncated and p.notes["edges_sent"] < p.notes["edges"]
    assert p.predicted == root


def test_public_api_exposes_no_benchmark_run_endpoint():
    from ic.api.app import create_app

    app = create_app()
    for route in app.routes:
        path = getattr(route, "path", "")
        methods = getattr(route, "methods", None) or set()
        if "bench" in path.lower():
            assert methods <= {"GET", "HEAD"}, (path, methods)
