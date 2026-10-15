# Benchmark details

Full results behind the summary in the [README](../README.md#benchmark).

From [api/bench_results/report.md](../api/bench_results/report.md). Run with
`uv run ic bench --seed 42` (Postgres twin included), `uv run ic bench --scale 4000`, and
`uv run ic learn --runs 6 --forget 4`.

50 injected faults (30 training-class, 20 held-out), each in its own isolated copy of the seed
world. A prediction is correct only when it names the exact root-cause entity id from the
answer key.

| Approach | Accuracy (seen) | Accuracy (held-out) | Overall | Mean ms / case | LLM tokens / case | Prompt tokens / case (est.) | Tool calls / case |
|---|---|---|---|---|---|---|---|
| graph (deterministic) | 100% | 100% | 100% (50/50) | 149 | 0 | 0 | 9.0 |
| vector_rag (hashing embedder, no LLM) | 23% | 0% | 14% (7/50) | 14 | 0 | 473 | 0.0 |
| full_context (heuristic, no LLM) | 23% | 15% | 20% (10/50) | 20 | 0 | 23828 | 0.0 |

| Approach | bad_deploy | config_change | flag_flip | expiry (held-out) | undeclared_coupling (held-out) |
|---|---|---|---|---|---|
| graph (deterministic) | 10/10 | 10/10 | 10/10 | 10/10 | 10/10 |
| vector_rag (hashing embedder, no LLM) | 1/10 | 4/10 | 2/10 | 0/10 | 0/10 |
| full_context (heuristic, no LLM) | 3/10 | 0/10 | 4/10 | 3/10 | 0/10 |

## Live LLM run

From [api/bench_results/llm-gpt-6-luna/report.md](../api/bench_results/llm-gpt-6-luna/report.md):
the same 50 cases (same seed, same case seeds), every approach driven by `gpt-6-luna` over the
OpenAI API. Run with `uv run ic bench --seed 42 --llm llm --no-latency` and a key in `api/.env`.
No verdict fell back to the deterministic funnel (0/50 DEGRADED) and no case errored. With an
OpenAI key, `vector_rag` embeds with `text-embedding-3-small` instead of the hashing embedder. The
whole run used 2.6M tokens.

| Approach | Accuracy (seen) | Accuracy (held-out) | Overall | Mean ms / case | LLM tokens / case | Tool calls / case |
|---|---|---|---|---|---|---|
| graph (llm: gpt-6-luna) | 100% | 100% | 100% (50/50) | 13022 | 22393 | 7.0 |
| vector_rag (llm: gpt-6-luna) | 3% | 15% | 8% (4/50) | 2446 | 570 | 0.0 |
| full_context (llm: gpt-6-luna) | 53% | 50% | 52% (26/50) | 1761 | 29019 | 0.0 |

| Approach | bad_deploy | config_change | flag_flip | expiry (held-out) | undeclared_coupling (held-out) |
|---|---|---|---|---|---|
| graph (llm: gpt-6-luna) | 10/10 | 10/10 | 10/10 | 10/10 | 10/10 |
| vector_rag (llm: gpt-6-luna) | 0/10 | 1/10 | 0/10 | 2/10 | 1/10 |
| full_context (llm: gpt-6-luna) | 7/10 | 4/10 | 5/10 | 6/10 | 4/10 |

The model is the same in every row. What changes is what it gets to look at. With graph tools
it calls 7 of them per case and every verdict still has to pass the proof gate. Given the
catalog as text, it reads about 24k tokens and guesses.

The graph agent does not need a frontier model. With `qwen3:8b` running locally on Ollama
([api/bench_results/llm-qwen3-8b/report.md](../api/bench_results/llm-qwen3-8b/report.md)) it also
named the exact root in **50/50** (20/20 held-out), at 6.5 tool calls and about 24 s per case,
with no fallback. A 5-case smoke with `claude-haiku-5-5` went 5/5
([api/bench_results/llm-smoke/](../api/bench_results/llm-smoke/)). "Fallback" counts verdicts the
deterministic funnel produced after the model failed (timeout, step cap); every live graph
verdict above is the model's own. A stronger model does not close the full-context gap either: on the
first 10 cases `claude-opus-5-5` with the whole catalog in its prompt got 4/10 (vector RAG
1/10), where the graph agent got 10/10 and `gpt-6-luna` full context got 7/10
([api/bench_results/llm-claude-opus-5-5/report.md](../api/bench_results/llm-claude-opus-5-5/report.md)). qwen3 runs with thinking off: with it on, a single tool step
took 11.7 s instead of 0.7 s and most steps ran past the 30 s per-call limit.

**Why it was wrong.** `uv run ic bench --why-wrong` re-checks every wrong live-LLM prediction
with `verify_claim` on the regenerated case world (no LLM calls), and `/benchmark` lists each
one with its failing clause and deciding query. For `gpt-6-luna`, vector RAG's 46 wrong answers
are 41 where the model named no root cause and 5 refuted on `origin` (the change's service was
not the first to fail). Full context's 24 are 19 refuted on `signature` (the change kind cannot
produce the observed symptoms, e.g. "config cannot produce latency"), 4 on `origin` and 1 on
`after_onset` (the change landed after the first symptom). For `claude-opus-5-5` on 10 cases,
all 6 full-context misses fail `signature`; vector RAG's 9 are 4 with no claim, 3 `origin`, 1
`after_onset` and 1 `signature`. Every named wrong answer (29 + 11) came back REFUTED; none was
a RIVAL that also qualified.

## Latency and scale

**Per-step latency, FalkorDB vs Postgres** (858 services, same host, warm cache, 50 runs each):

| Operation | FalkorDB p50 ms | FalkorDB p95 ms | Postgres p50 ms | Postgres p95 ms | Results agree |
|---|---|---|---|---|---|
| Upstream closure (algo.BFS vs recursive CTE, valid-time filter) | 1.11 | 1.39 | 1.78 | 5.83 | 50/50 |
| Shortest path (shortestPath vs recursive CTE) | 0.79 | 1.62 | 1.77 | 6.81 | 50/50 |
| Top-5 precedent (vector index vs pgvector HNSW) | 1.32 | 1.60 | 0.71 | 0.92 | 50/50 |

**Scale** (`generate_world(42, n_services=4000)`: 8,335 services, 15,075 DEPENDS_ON, 15,678
OBSERVED_CALL, 54,051 changes):

| Operation | FalkorDB p50 ms | FalkorDB p95 ms | Postgres p50 ms | Postgres p95 ms | Results agree |
|---|---|---|---|---|---|
| Upstream closure, depth 2 | 3.15 | 3.75 | 5.62 | 6.17 | 50/50 |
| Upstream closure, depth 4 | 3.26 | 3.89 | 8.73 | 8.95 | 50/50 |
| Upstream closure, depth 8 | 4.04 | 4.95 | 9.74 | 12.98 | 50/50 |
| Shortest path to the farthest reachable service | 1.01 | 1.44 | 10.16 | 13.45 | 50/50 |

FalkorDB queries a materialized snapshot (built in 0.6 s, listed separately in the report).
Postgres applies the valid-time filter inside each recursive query. The generated topology is
tiered, so it is wide rather than deep: the farthest target any sampled source reaches is 5
hops away.

**Learning curve and forgetting curve** (`uv run ic learn --runs 6 --forget 4`,
[api/bench_results/learning_curve.json](../api/bench_results/learning_curve.json)). 6
undeclared-coupling incidents run in sequence in one session world, with memory write-back
after each. Then a deploy reroutes the caller through a declared replica, so the learned
coupling `svc-catalog-events@eu-west -> svc-elasticsearch-users@eu-west` stops being true, and
each later incident's clean explanation contradicts it:

| # | Incident | Phase | Verdict | Tool calls | Recall seeded | Learned edge after |
|---|---|---|---|---|---|---|
| 1 | inc-041 | learn | DIAGNOSED | 9 | no | created (0.60) |
| 2 | inc-042 | learn | DIAGNOSED | 5 | yes | confirmed (0.80) |
| 3 | inc-043 | learn | DIAGNOSED | 5 | yes | confirmed (0.90) |
| 4 | inc-044 | learn | DIAGNOSED | 5 | yes | confirmed (0.95) |
| 5 | inc-045 | learn | DIAGNOSED | 5 | yes | confirmed (0.975) |
| 6 | inc-046 | learn | DIAGNOSED | 5 | yes | confirmed (0.9875) |
| 7 | inc-047 | forget | DIAGNOSED | 5 | yes | contradicted (0.494) |
| 8 | inc-048 | forget | DIAGNOSED | 5 | yes | contradicted (0.247) |
| 9 | inc-049 | forget | DIAGNOSED | 9 | no | contradicted (0.123) |

Incident 1 took 9 tool calls; incidents 2-6 took 5 each (44% fewer) while the edge's confidence
rose from 0.6 to 0.9875. Two contradictions halved it to 0.247, under the 0.25 recall
threshold, so incident 9 was not memory-seeded and went back to 9 tool calls. All 9 were
correct. Incident 1 is the first acceptable attempt, not the first attempt: one attempt that
had too few downstream alert services was discarded and is listed in the JSON.

**Read these numbers with care:**

- **Two runs, two settings.** The headline table, with latency, scale and learning curve, comes
  from a run with no LLM. There "graph" is the deterministic graph agent (the KTD23 funnel plus
  the proof chain, same verdict gate) and the baselines are stand-ins: `vector_rag` picks the
  nearest candidate by hashing-embedder cosine similarity, and `full_context` picks the most
  recent change before the first symptom on the alert service or its declared direct
  dependencies. The [live LLM run](#live-llm-run) repeats the same 50 cases with
  `gpt-6-luna` behind all three approaches.
- **Graph accuracy is partly true by construction.** Each injected fault is verified at
  injection time against the same explanation rule the agent uses, and decoys are generated to
  fail at least one clause. The 100% shows the agent finds and proves the unique qualifying
  entity among timing-matched decoys. It does not show the predicate models real outages. The
  leakage test and held-out classes check that no shortcut gives the answer away. They do not
  remove this circularity.
- Held-out classes (expiry, undeclared coupling) were written after the triage prompts and tool
  descriptions, which never name them.
- Seed 42, case seeds and commit hashes are listed in `report.json`.
