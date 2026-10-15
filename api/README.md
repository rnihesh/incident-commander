# Incident Commander API

FastAPI backend for Incident Commander. It owns the FalkorDB world graph, runs the
investigation agent, stores hash-chained proofs, and serves the MCP tools.

See the [root README](../README.md) for the full story, graph data model and Cypher
reference.

## Run

```bash
docker compose --profile dev up -d      # from the repo root: FalkorDB on localhost:16379
uv sync
uv run ic seed --seed 42                # generate and load the synthetic world (~6 s)
IC_COOKIE_SECURE=false uv run uvicorn ic.api.app:app --port 8000
```

Settings are `IC_*` environment variables (or `api/.env`); `../.env.example` lists them all.
With no LLM key the deterministic graph agent runs the same tools and proofs.

## CLI (`uv run ic ...`)

| Command | What it does |
|---|---|
| `seed` | Generate the synthetic company world (default 400 services) and load it into FalkorDB |
| `inject --class <fault>` | Inject a training-class fault into a session world (`--reveal` prints the answer key) |
| `mcp` | MCP server on stdio with the read-only graph tools (`verify_claim`, company tools) |
| `bench` | Run the blind-fault benchmark (graph agent vs vector RAG vs full context) and write `bench_results/` |
| `learn` | Learning curve: sequential incidents in one session with memory write-back (`--forget N` adds the forgetting phase) |
| `record-hero` | Record the fallback "hero" run that the UI autoplays |

## Layout

| Package | Responsibility |
|---|---|
| `ic.graph` | FalkorDB client, graph naming, schema, as-of snapshots and `IMPACTS` paths |
| `ic.world` | World generator, fault injection (with decoys and storms), causality oracle, loader |
| `ic.tools` | Agent tool registry: explanation predicate, investigate, reflex, precedent, recall, rehearse, storm, claims, fragility |
| `ic.proof` | Hash-chained proof ledger, canonical encoding, verification |
| `ic.agent` | LLM adapter, agent loop, triage and remediation agents, prompts |
| `ic.memory` | `LEARNED_DEPENDS_ON` write-back and postmortem recommendations |
| `ic.actions` | GitHub rollback PR and commit status, Discord pager, who-to-page |
| `ic.sessions`, `ic.runs`, `ic.incidents` | Per-visitor worlds, abuse limits, run orchestration, incident state machine |
| `ic.api` | HTTP routes, SSE events, judge / challenge / replay / storm / proof endpoints |
| `ic.bench` | Benchmark runner, baselines, Postgres twin, scale and learning-curve reports |

## Test

```bash
uv run ic seed --seed 42    # tests copy seed:world, so seed once first
uv run pytest -q            # needs FalkorDB on localhost:16379; live/slow markers skipped
uv run ruff check .
```
