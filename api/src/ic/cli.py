"""``ic`` command line. ``ic seed`` writes the synthetic world; ``ic inject`` injects a fault;
``ic record-hero`` records the fallback run served when limits refuse a live one; ``ic bench``
runs the benchmark locally (U15; ``--scale N`` adds the large-world FalkorDB vs Postgres
comparison); ``ic learn`` runs the memory learning curve (U13)."""

from __future__ import annotations

import argparse
import asyncio
import time

from ic.graph import client, naming
from ic.graph.snapshots import create_session_world
from ic.store import Store
from ic.world.faults import TRAINING_CLASSES, inject_fault
from ic.world.generator import generate_world
from ic.world.loader import SEED_GRAPH, load_world
from ic.world.model import world_content_hash


async def _seed(seed: int, services: int, graph: str) -> None:
    t0 = time.perf_counter()
    world = generate_world(seed, services)
    t1 = time.perf_counter()
    print(f"generated in {t1 - t0:.1f}s")
    for label, n in world.node_counts().items():
        print(f"  {label:<15}{n}")
    print(f"  edges          {sum(world.edge_counts().values())}")
    await load_world(world, graph)
    print(f"loaded into '{graph}' in {time.perf_counter() - t1:.1f}s")
    print(f"content hash: {world_content_hash(world)}")


async def _inject(fault_class: str, seed: int, session: str | None, reveal: bool) -> None:
    sid = session or naming.new_session_id()
    world = naming.session_world(sid)
    try:
        if not await client.graph_exists(world):
            await create_session_world(sid)
            print(f"created session world {world}")
        fault = await inject_fault(
            world, session_id=sid, fault_class=fault_class, rng_seed=seed, store=Store()
        )
    finally:
        client.reset_client()
    print(f"session:       {sid}")
    print(f"incident:      {fault.incident_id}")
    print(f"alert service: {fault.alert_service_id}")
    print(f"alert time:    {fault.alert_time}")
    if reveal:
        a = fault.answer
        print(f"ANSWER class={a.fault_class} root={a.root_id} nonce={a.nonce}")


async def _record_hero(path: str | None, llm_mode: str = "none") -> None:
    from pathlib import Path

    from ic.bench.runner import resolve_llm
    from ic.runs.recorded import HERO_PATH, record_hero

    out = Path(path) if path else HERO_PATH
    try:
        events = await record_hero(out, llm=resolve_llm(llm_mode))
    finally:
        client.reset_client()
    verdict = next((e["data"] for e in events if e["type"] == "verdict"), {})
    print(f"recorded {len(events)} events to {out}")
    print(f"verdict: {verdict.get('outcome')} {verdict.get('suspect_id')}")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="ic")
    sub = parser.add_subparsers(dest="command", required=True)
    seed = sub.add_parser("seed", help="generate the synthetic world and load it into FalkorDB")
    seed.add_argument("--seed", type=int, default=42)
    seed.add_argument("--services", type=int, default=400)
    seed.add_argument("--graph", default=SEED_GRAPH)
    inject = sub.add_parser("inject", help="inject a training-class fault into a session world")
    inject.add_argument(
        "--class", dest="fault_class", choices=sorted(TRAINING_CLASSES), required=True
    )
    inject.add_argument("--seed", type=int, default=0)
    inject.add_argument("--session", default=None, help="session id (default: new session)")
    inject.add_argument("--reveal", action="store_true", help="also print the answer key")
    rec = sub.add_parser("record-hero", help="record the fallback hero run's events")
    rec.add_argument("--out", default=None, help="output path (default: packaged hero.json)")
    rec.add_argument(
        "--llm",
        choices=["auto", "none", "llm"],
        default="none",
        help="none: deterministic agent; llm: record a live run with the configured provider",
    )
    mcp = sub.add_parser("mcp", help="MCP server on stdio (read-only graph tools)")
    mcp.add_argument("--session", default=None, help="local session id (default: seed:world only)")
    mcp.add_argument("--incident", default=None, help="incident whose snapshots the tools use")
    bench = sub.add_parser("bench", help="run the benchmark locally and write the report")
    bench.add_argument("--seed", type=int, default=42)
    bench.add_argument("--cases", type=int, default=50)
    bench.add_argument("--approaches", default="graph,vector_rag,full_context")
    bench.add_argument("--out", default=None, help="report directory (default: api/bench_results)")
    bench.add_argument(
        "--llm",
        choices=["auto", "none", "llm"],
        default="auto",
        help="auto: use the configured LLM if a key is set; none: deterministic modes; "
        "llm: require a key",
    )
    bench.add_argument("--latency-iters", type=int, default=50)
    bench.add_argument("--no-latency", action="store_true", help="skip the Postgres twin")
    bench.add_argument(
        "--scale",
        type=int,
        default=None,
        metavar="N",
        help="run only the scale comparison on generate_world(seed, n_services=N) and merge "
        "it into the existing report",
    )
    bench.add_argument(
        "--learning-curve",
        type=int,
        default=None,
        metavar="N",
        help="run only the N-incident learning curve (same as `ic learn --runs N`)",
    )
    bench.add_argument(
        "--attach-llm",
        action="append",
        default=None,
        metavar="REPORT",
        help="attach a finished live-LLM run (report dir or .json) to the existing report as "
        "live_llm_runs; repeatable, no benchmark is run",
    )
    bench.add_argument(
        "--why-wrong",
        action="store_true",
        help="re-check every wrong prediction of the attached live-LLM runs with verify_claim "
        "on regenerated case worlds (after --attach-llm when both are given); no LLM calls",
    )
    learn = sub.add_parser(
        "learn", help="learning curve: sequential incidents in one session with memory"
    )
    learn.add_argument("--runs", type=int, default=6)
    learn.add_argument(
        "--forget",
        type=int,
        default=0,
        metavar="N",
        help="then remove the learned coupling from the world and run up to N incidents that "
        "contradict it, until recall stops seeding it (0: no forget phase)",
    )
    learn.add_argument("--seed", type=int, default=42)
    learn.add_argument("--out", default=None, help="report directory (default: api/bench_results)")
    learn.add_argument("--llm", choices=["auto", "none", "llm"], default="none")
    args = parser.parse_args(argv)
    if args.command == "bench" and args.learning_curve is not None:
        args.command, args.runs = "learn", args.learning_curve
    if args.command in ("bench", "learn"):
        from ic.bench import runner

        if args.out is None:
            args.out = runner.DEFAULT_OUT
        if args.command == "learn":
            from ic.bench import learning_curve

            learning_curve.main(args)
        elif args.attach_llm or args.why_wrong:
            from ic.bench import live_llm, why_wrong

            if args.attach_llm:
                live_llm.main(args)
            if args.why_wrong:
                why_wrong.main(args)
        elif args.scale is not None:
            from ic.bench import scale

            scale.main(args)
        else:
            runner.main(args)
        return
    if args.command == "seed":
        asyncio.run(_seed(args.seed, args.services, args.graph))
    elif args.command == "inject":
        asyncio.run(_inject(args.fault_class, args.seed, args.session, args.reveal))
    elif args.command == "mcp":
        from ic.mcp_server import stdio_server

        try:
            server = stdio_server(args.session, args.incident)
        except ValueError as e:
            parser.error(str(e))
        server.run("stdio")
    elif args.command == "record-hero":
        asyncio.run(_record_hero(args.out, args.llm))


if __name__ == "__main__":
    main()
