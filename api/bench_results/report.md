# Incident Commander benchmark

50 injected faults (30 training-class, 20 held-out), each in its own isolated copy of the seed world. A prediction is correct when it names the exact root-cause entity id from the answer key.

## Accuracy, time and tokens

| Approach | Accuracy (seen) | Accuracy (held-out) | Overall | Mean ms / case | LLM tokens / case | Prompt tokens / case (est.) | Tool calls / case |
|---|---|---|---|---|---|---|---|
| full_context (heuristic, no LLM) | 23% | 15% | 20% (10/50) | 20 | 0 | 23828 | 0.0 |
| graph (deterministic) | 100% | 100% | 100% (50/50) | 149 | 0 | 0 | 9.0 |
| vector_rag (hashing embedder, no LLM) | 23% | 0% | 14% (7/50) | 14 | 0 | 473 | 0.0 |

## Correct per fault class

| Approach | bad_deploy | config_change | flag_flip | expiry (held-out) | undeclared_coupling (held-out) |
|---|---|---|---|---|---|
| full_context (heuristic, no LLM) | 3/10 | 0/10 | 4/10 | 3/10 | 0/10 |
| graph (deterministic) | 10/10 | 10/10 | 10/10 | 10/10 | 10/10 |
| vector_rag (hashing embedder, no LLM) | 1/10 | 4/10 | 2/10 | 0/10 | 0/10 |

## Per-step latency: FalkorDB vs Postgres

Same world (seed:world, 858 services, 1569 DEPENDS_ON, 1632 OBSERVED_CALL, 40 postmortems), same host, warm cache, 50 timed runs per operation and engine. FalkorDB runs on a materialized as-of snapshot; Postgres filters valid time inside the query.

| Operation | FalkorDB p50 ms | FalkorDB p95 ms | Postgres p50 ms | Postgres p95 ms | Results agree |
|---|---|---|---|---|---|
| Upstream closure (algo.BFS vs recursive CTE, valid-time filter) | 1.11 | 1.39 | 1.78 | 5.83 | 50/50 |
| Shortest path (shortestPath vs recursive CTE) | 0.79 | 1.62 | 1.77 | 6.81 | 50/50 |
| Top-5 precedent (vector index vs pgvector HNSW) | 1.32 | 1.60 | 0.71 | 0.92 | 50/50 (exact top-k vs brute force: FalkorDB 50/50, Postgres 50/50) |

Closure sets and shortest-path lengths are compared input by input; top-k lists are compared in order. Both vector indexes are approximate (HNSW), so top-k lists can differ where a neighbour is missed; each engine is also checked against an exact brute-force ranking.

## Scale: FalkorDB vs Postgres on a large world

`generate_world(42, n_services=4000)`: 8335 services, 15075 DEPENDS_ON, 15678 OBSERVED_CALL, 54051 changes (15527 Service->Service IMPACTS edges in the as-of snapshot). 50 timed runs per operation and engine.

| Operation | FalkorDB p50 ms | FalkorDB p95 ms | Postgres p50 ms | Postgres p95 ms | Mean result | Results agree |
|---|---|---|---|---|---|---|
| Upstream closure, depth 2 (algo.BFS vs recursive CTE) | 3.15 | 3.75 | 5.62 | 6.17 | 26.3 services | 50/50 (vs BFS: FalkorDB 50, Postgres 50) |
| Upstream closure, depth 4 (algo.BFS vs recursive CTE) | 3.26 | 3.89 | 8.73 | 8.95 | 40.5 services | 50/50 (vs BFS: FalkorDB 50, Postgres 50) |
| Upstream closure, depth 8 (algo.BFS vs recursive CTE) | 4.04 | 4.95 | 9.74 | 12.98 | 41.0 services | 50/50 (vs BFS: FalkorDB 50, Postgres 50) |
| Shortest path to the farthest reachable service (shortestPath vs CTE) | 1.01 | 1.44 | 10.16 | 13.45 | 3.52 hops | 50/50 (vs BFS: FalkorDB 50, Postgres 50) |

Setup (not part of the timings): generate 100.7 s, load falkordb 4.1 s, load postgres 0.4 s, read rows 1.6 s, snapshot 0.6 s.

- Sources: the 50 deepest of 400 randomly drawn services (by services reached at the snapshot time); path targets are the farthest service each source reaches.
- Agree = FalkorDB and Postgres return the same set (closure) or hop count (path) for the same input; both are also checked against an in-memory BFS over the same edges (reference in scale.json).
- FalkorDB queries a materialized as-of snapshot (IMPACTS edges built ahead of time, snapshot build time listed separately); Postgres applies the valid-time filter inside each recursive query. Same host, warm cache, one client.
- Top-k precedent search is not repeated: postmortems do not grow with service count (1 in this world).
- Traversal depth actually reached: the farthest target of any sampled source is 5 hops (mean 3.52). Mean services reached (depth 2: 26.3, depth 4: 40.5, depth 8: 41.0). The generator's topology is tiered, so this world is wide rather than deep.

Seed 42 · commit `22bf7f3c1f6781810e9a7961644e8f567359c73d`.

## Learning curve

6 undeclared-coupling incidents investigated in sequence in ONE session world, memory written back after each resolution (KTD15: separate from the accuracy runs, which reset memory per case). Incident 1 teaches `svc-catalog-events@eu-west -> svc-elasticsearch-users@eu-west`; every later fault is rooted on `svc-elasticsearch-users@eu-west` and its symptoms travel through that coupling to a different alert service where possible. Triage: none (deterministic triage).

| # | Incident | Alert service | Verdict | Tool calls | LLM steps | Wall ms | Recall seeded | Learned edge after |
|---|---|---|---|---|---|---|---|---|
| 1 | inc-041 | `svc-tracking-bff@us-east` | DIAGNOSED | 9 | 0 | 1237 | no | created (0.60) |
| 2 | inc-042 | `svc-commerce-gateway-5@ap-south` | DIAGNOSED | 5 | 0 | 1263 | yes | confirmed (0.80) |
| 3 | inc-043 | `svc-tracking-bff@ap-south` | DIAGNOSED | 5 | 0 | 1260 | yes | confirmed (0.90) |
| 4 | inc-044 | `svc-pricing-api@eu-west` | DIAGNOSED | 5 | 0 | 1157 | yes | confirmed (0.95) |
| 5 | inc-045 | `svc-tax-api@us-east` | DIAGNOSED | 5 | 0 | 1222 | yes | confirmed (0.97) |
| 6 | inc-046 | `svc-tax-api@eu-west` | DIAGNOSED | 5 | 0 | 1282 | yes | confirmed (0.99) |
| 7 | inc-047 | `svc-api-gateway-3@eu-west` | DIAGNOSED | 5 | 0 | 1261 | yes | contradicted (0.49) · forget |
| 8 | inc-048 | `svc-tracking-worker@us-east` | DIAGNOSED | 5 | 0 | 1239 | yes | contradicted (0.25) · forget |
| 9 | inc-049 | `svc-tracking-worker@eu-west` | DIAGNOSED | 9 | 0 | 1325 | no | contradicted (0.12) · forget |

Tool calls: incident 1 = 9, incident 2 = 5, mean of incidents 2-9 = 5 (44.4% fewer). 6/6 correct.

Forget phase: after chg-05725 (Deploy v2.17.0: read svc-elasticsearch-users@eu-west through svc-audit-log-replica@eu-west instead of calling it directly), 3 incident(s) contradicted the learned edge, each halving its confidence; recall seeds only from edges at or above 0.25. Edge confidence per incident: 0.600, 0.800, 0.900, 0.950, 0.975, 0.988, 0.494, 0.247, 0.123. inc-049 ran with the edge under the threshold: recall no longer seeded from it (9 tool calls).

One session world, incidents investigated in sequence with write-back after each RESOLVED run. Incident 1 is selected by retry-until-correct: attempts that were wrong, taught no coupling, or had too few downstream alert services are discarded and listed in discarded_incident1_attempts (the first incident shown is the first acceptable attempt, not the first attempt). Incident 1 teaches LEARNED_DEPENDS_ON svc-catalog-events@eu-west -> svc-elasticsearch-users@eu-west; later faults are rooted on svc-elasticsearch-users@eu-west and their symptoms include both ends of the coupling (seeds probed on a scratch copy first). Tool calls count every registry call of the triage run (recall, screening, proofs and the verdict's causal path). Forget phase: chg-05725 (a deploy of svc-catalog-events@eu-west) stops calling svc-elasticsearch-users@eu-west directly and goes through svc-audit-log-replica@eu-west (declared), and the observed-call edge is removed; each later incident is a training fault whose symptoms include both ends (seeds probed first), so both are symptomatic through another path, the cleared explanation uses no hop through the coupling and the write-back halves the learned edge's confidence. It runs until the confidence is under the recall threshold 0.25 plus one more incident. edge_confidence is read from the world after every incident.

Seeds [42001, 42101, 42106, 42117, 42126, 42134] · commit `2e6993823905a2106a7ce22c5950ede9d6c28ef5`.

## Live LLM runs

Separate runs with a real language model behind each approach (the headline table above is the deterministic run, no LLM). Same fault generator and answer key. Fallback counts verdicts the deterministic funnel produced after the model failed.

| Model | Cases | Approach | Overall | Seen | Held-out | LLM tokens / case | Tool calls / case | Mean ms / case | Fallback |
|---|---|---|---|---|---|---|---|---|---|
| claude-fable-5-1 | 50 | graph | 100% (50/50) | 100% | 100% | 44590 | 7.0 | 18371 | 0/50 |
| claude-fable-5-1 | 50 | vector_rag | 12% (6/50) | 10% | 15% | 1036 | 0.0 | 3902 | - |
| claude-fable-5-1 | 50 | full_context | 52% (26/50) | 47% | 60% | 48713 | 0.0 | 8125 | - |
| claude-haiku-5-5 | 50 | graph | 100% (50/50) | 100% | 100% | 44353 | 7.0 | 8479 | 0/50 |
| claude-haiku-5-5 | 50 | vector_rag | 10% (5/50) | 13% | 5% | 1182 | 0.0 | 2582 | - |
| claude-haiku-5-5 | 50 | full_context | 30% (15/50) | 20% | 45% | 50130 | 0.0 | 5458 | - |
| claude-opus-5-5 | 50 | graph | 100% (50/50) | 100% | 100% | 38334 | 6.3 | 12522 | 1/50 |
| claude-opus-5-5 | 50 | vector_rag | 16% (8/50) | 17% | 15% | 1061 | 0.0 | 3756 | - |
| claude-opus-5-5 | 50 | full_context | 54% (27/50) | 47% | 65% | 49617 | 0.0 | 6147 | - |
| claude-sonnet-5-5 | 50 | graph | 100% (50/50) | 100% | 100% | 43869 | 7.0 | 11008 | 0/50 |
| claude-sonnet-5-5 | 50 | vector_rag | 10% (5/50) | 10% | 10% | 1051 | 0.0 | 3215 | - |
| claude-sonnet-5-5 | 50 | full_context | 38% (19/50) | 23% | 60% | 49992 | 0.0 | 7270 | - |
| gpt-6-luna | 50 | graph | 100% (50/50) | 100% | 100% | 22393 | 7.0 | 13022 | 0/50 |
| gpt-6-luna | 50 | vector_rag | 8% (4/50) | 3% | 15% | 570 | 0.0 | 2446 | - |
| gpt-6-luna | 50 | full_context | 52% (26/50) | 53% | 50% | 29019 | 0.0 | 1761 | - |
| qwen3:8b | 50 | graph | 100% (50/50) | 100% | 100% | 24998 | 6.5 | 23524 | 0/50 |

Runs: claude-fable-5-1 (50 cases, commit `0ef6f6c9ba26`, 2026-10-18T10:49:35Z); claude-haiku-5-5 (50 cases, commit `0ef6f6c9ba26`, 2026-10-18T09:45:31Z); claude-opus-5-5 (50 cases, commit `0ef6f6c9ba26`, 2026-10-18T10:23:48Z); claude-sonnet-5-5 (50 cases, commit `0ef6f6c9ba26`, 2026-10-18T10:04:31Z); gpt-6-luna (50 cases, commit `76921df63c47`, 2026-10-09T15:36:16Z); qwen3:8b (50 cases, commit `1657c1f02e0d`, 2026-10-09T16:34:00Z).

## Why it was wrong

Every wrong prediction in the live runs above, re-checked with `verify_claim` (the funnel's own predicate) on its case world regenerated from the seed that case used. Columns are the first clause the predicted id fails; no_claim means the approach named no id.

| Model | Cases | Approach | Wrong | after_onset | origin | signature | no_claim |
|---|---|---|---|---|---|---|---|
| claude-fable-5-1 | 50 | full_context | 24 | - | 3 | 20 | 1 |
| claude-fable-5-1 | 50 | vector_rag | 44 | 1 | 21 | 6 | 16 |
| claude-haiku-5-5 | 50 | full_context | 35 | - | 9 | 26 | - |
| claude-haiku-5-5 | 50 | vector_rag | 45 | 1 | 24 | 4 | 16 |
| claude-opus-5-5 | 50 | full_context | 23 | - | 2 | 21 | - |
| claude-opus-5-5 | 50 | vector_rag | 42 | 1 | 20 | 5 | 16 |
| claude-sonnet-5-5 | 50 | full_context | 31 | - | 7 | 24 | - |
| claude-sonnet-5-5 | 50 | vector_rag | 45 | 1 | 22 | 6 | 16 |
| gpt-6-luna | 50 | vector_rag | 46 | - | 5 | - | 41 |
| gpt-6-luna | 50 | full_context | 24 | 1 | 4 | 19 | - |

| Model | Case | Fault class | Approach | Predicted | Verdict | Why |
|---|---|---|---|---|---|---|
| claude-fable-5-1 | c000 | undeclared_coupling | full_context | `chg-05692` | REFUTED · signature | deploy cannot produce saturation |
| claude-fable-5-1 | c000 | undeclared_coupling | vector_rag | `chg-05692` | REFUTED · signature | deploy cannot produce saturation |
| claude-fable-5-1 | c002 | config_change | vector_rag | `chg-05690` | REFUTED · origin | svc-promotions-sync@us-east was not the first service to fail |
| claude-haiku-5-5 | c000 | undeclared_coupling | full_context | `chg-05692` | REFUTED · signature | deploy cannot produce saturation |
| claude-haiku-5-5 | c000 | undeclared_coupling | vector_rag | `chg-05692` | REFUTED · signature | deploy cannot produce saturation |
| claude-haiku-5-5 | c002 | config_change | vector_rag | `chg-05690` | REFUTED · origin | svc-promotions-sync@us-east was not the first service to fail |
| claude-opus-5-5 | c000 | undeclared_coupling | full_context | `chg-05692` | REFUTED · signature | deploy cannot produce saturation |
| claude-opus-5-5 | c000 | undeclared_coupling | vector_rag | `chg-05692` | REFUTED · signature | deploy cannot produce saturation |
| claude-opus-5-5 | c002 | config_change | vector_rag | `chg-05690` | REFUTED · origin | svc-promotions-sync@us-east was not the first service to fail |
| claude-sonnet-5-5 | c000 | undeclared_coupling | full_context | `chg-05692` | REFUTED · signature | deploy cannot produce saturation |
| claude-sonnet-5-5 | c000 | undeclared_coupling | vector_rag | `chg-05692` | REFUTED · signature | deploy cannot produce saturation |
| claude-sonnet-5-5 | c002 | config_change | vector_rag | `chg-05690` | REFUTED · origin | svc-promotions-sync@us-east was not the first service to fail |
| gpt-6-luna | c003 | flag_flip | full_context | `chg-05691` | REFUTED · signature | config cannot produce latency |
| gpt-6-luna | c007 | config_change | full_context | `chg-05689` | REFUTED · signature | deploy cannot produce saturation |
| gpt-6-luna | c007 | config_change | vector_rag | `chg-05690` | REFUTED · origin | svc-analytics-api-batch@ap-south was not the first service to fail |

Every row with its deciding query is in report.json (`live_llm_runs[].why_wrong`).

## Notes

- No LLM key was configured: graph = deterministic graph fallback (KTD23 funnel + proof chain over IMPACTS, verdict gate unchanged); vector_rag = nearest candidate chunk by HashingEmbedder cosine similarity; full_context = heuristic stand-in for an LLM without graph tools: most recent change/expiry before the first symptom on the alert service or its declared direct dependencies. LLM-backed modes are wired (ic bench --llm auto) and need a key.
- Correct = exact root entity id (change or expiry) from the answer key; an INCONCLUSIVE graph verdict makes no claim and counts as not correct.
- Held-out classes (expiry, undeclared_coupling) were written after the triage prompts and tool descriptions, which do not change in this unit and never name them as fault classes (tested; Expiry as a graph label and the observed_calls tool predate them).
- Every approach reads the same as-of snapshot (known view at alert time) and the same candidate window [earliest onset - 120 min, alert time].
- full_context serializes the declared service catalog (DEPENDS_ON only, no OBSERVED_CALL); prompt tokens are estimated as len(text)/4 against a 128,000-token limit.
- Mean ms is wall time per case for the approach alone (world copy, injection and snapshot builds excluded).

---

Seed 42 · case seeds 53b6b393b51d (full list in report.json) · commit `cc9592cf0cd599a7fed075d6f381fa50ceaf479a` · 50 cases · LLM: none (deterministic/heuristic modes)
