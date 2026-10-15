# Feature tour

Every feature, what it does, and the FalkorDB capability it depends on.

| # | Feature | What it does | FalkorDB capability that makes it work |
|---|---|---|---|
| 1 | **Proof-carrying RCA** | Each hypothesis is written to the ledger graph *before* its falsifier runs (`REGISTERED`). It then becomes `HOLDS` or `REFUTED`, with a `Proof` node chained by `prev_hash`. A verdict needs three HOLDS proofs for the suspect: qualifies, necessary, no other qualifying. Verify rebuilds snapshots from the current world and re-runs every proof, giving VERIFIED, DRIFTED, FALSIFIED, TAMPERED or UNANCHORED. The chain head is anchored in SQLite. | Cypher-only predicate (`shortestPath` inside `WITH`, list comprehensions, `reduce`) that re-derives its verdict from the graph. `GRAPH.RO_QUERY` for every tool read. Separate ledger graph per session. |
| 2 | **Judge chaos monkey** | Blind commit-reveal injection on any service with an alert path. The in-app tamper panel can add an upstream deploy, remove a dependency edge, or move a change's timestamp. Verify then flips the affected proofs, and Re-derive runs a fresh investigation on the tampered world. Locally you can tamper through FalkorDB Browser instead ([docs/notes/browser-tamper.md](notes/browser-tamper.md)). | Per-session world graphs (`s:{sid}:world`). Parameterized writes. Redis ACL key patterns for a Browser judge user. |
| 3 | **Time machine** | Bitemporal facts (`valid_from`, `valid_to`, `recorded_at`). Each investigation gets two snapshots: `:true` (valid at T) and `:known` (valid *and recorded* by T). Verdicts use `:known`, and records written after the alert are flagged late-arriving. A replay scrubber (`/replay/{incident}`) plays the outage with an as-happened / as-known toggle. | `GRAPH.COPY` (about 67 ms for a 20k-node / 100k-edge graph in our spike) plus pruning, and derived `IMPACTS` edges via `MERGE`. |
| 4 | **Scar-tissue memory** | At RESOLVED, after a CLEARED rehearsal, the write-back adds `CAUSED_BY`, a rollback `Change`, a postmortem with an embedding, and `LEARNED_DEPENDS_ON` edges for each observed-but-undeclared hop on the causal path. `recall` uses learned edges and path-shape precedents to seed the next triage. Learned edges never become proof premises. | Atomic `MERGE ... ON CREATE SET ... ON MATCH SET` for confidence updates. Vector index on postmortems. |
| 5 | **Storm collapse** | A storm fires 200-400 pages from 2-3 independent faults. The agent groups them into incidents and finds the minimal explaining set per incident by greedy set cover. It flags joint flag+config causes using `GATED_BY` plus symptom-free solo intervals, and names chokepoints. Verified run: 323 pages became 2 incidents, 1 of them jointly caused. | `algo.WCC` over an alert-candidate bipartite scratch graph. Seeded `algo.betweenness`. |
| 6 | **Multiverse rollback rehearsal** | The remediation agent clones the as-true snapshot, ends the change's validity, restores its `PREVIOUS_VERSION`, and re-checks every other candidate in full and in part. It refuses to act, naming the surviving explanation, if anything still explains some of the symptoms. | `GRAPH.COPY` into `s:{sid}:rehearse:{iid}`. Writes on the clone only. The clone is deleted in `finally`. |
| 7 | **Postgres twin + reflex** | *Reflex:* pure Cypher on the session world before any snapshot or LLM call returns blast radius, search space, top suspects and on-call, labelled pre-snapshot (about 2 ms measured). *Twin:* the latency and scale tables below. | Valid-time-filtered variable-length paths at small hop bounds. `algo.BFS` and `shortestPath` vs recursive CTEs. |
| 8 | **Refutation check (`verify_claim`)** | One read-only tool judges any claimed root cause with the agent's own predicate on the incident's as-of snapshot: VERIFIED (qualifies, nothing else does), RIVAL (another candidate also qualifies) or REFUTED (the first clause it fails and the index of the query that decided it). It writes no proof, ledger entry or memory. The benchmark, the judge challenge, the fragility map and MCP clients all call it, so they agree by construction. | The same `EXPLAINS` Cypher as the funnel, through `GRAPH.RO_QUERY`. |
| 9 | **Judge challenge** | The console drawer's "Was it my deploy?" picks any change the investigation eliminated and re-checks it live on a fresh as-of build of the session world (so it sees judge tampering). Two challenges per incident; naming the diagnosed root is free. A challenge never changes the incident: a qualifying rival only reports `would_overturn: INCONCLUSIVE`. | `GRAPH.COPY` snapshot build per challenge, deleted afterwards. |
| 10 | **Query X-ray** | "X-ray" next to any funnel cut's or proof's query shows FalkorDB's plan and a live profile of that exact query (re-derived from the run's stored tool calls) on the incident snapshot: operator tree, records and ms per operator, total time. A note explains why the product snapshots instead of filtering valid time inside a variable-length match. | `GRAPH.EXPLAIN` and `GRAPH.PROFILE` with a 5 s server-side timeout. |
| 11 | **Clause bars and onset strip** | The absence certificate gives each disqualified change a five-segment bar in predicate clause order (passed, first failure, not reached), clause chips to filter by failing clause, and a strip placing every change against the earliest onset. | Each row is a stored predicate result; nothing is recomputed in the browser. |
| 12 | **Forgetting curve** | `ic learn --forget` changes the world so the learned coupling stops being true (the caller now reaches the callee through a declared replica), then runs incidents whose clean explanation contradicts the learned edge. Each contradiction halves its confidence; below 0.25 `recall` stops seeding it. The console and `/benchmark` chart tool calls and confidence per incident. | `MERGE ... ON MATCH SET` confidence updates on `LEARNED_DEPENDS_ON`. |
| 13 | **Fragility map** | Judge mode's "Try these" tries at most ten single edits (remove a causal-path hop, add an upstream deploy, move the root after onset), each on its own throwaway copy of the as-known snapshot, and lists the ones that flip the verdict with the clause they break. One click pre-fills the tamper form with that edit. | `GRAPH.COPY` per edit (about 130 ms each on the seed world), `verify_claim` on the copy, copies deleted in `finally`. |
| 14 | **Autopilot first paint** | A first-time visitor sees the recorded `gpt-6-luna` hero run play on its own, with its saved incident graph, instead of an empty console. Pressing **Inject fault** takes over. | Recorded run and graph served from `/api/recorded/hero` and `/api/recorded/hero/graph`. |
| 15 | **Ask the graph (Company Brain)** | The console drawer's **Ask the graph** tab answers plain-English questions about the company in the graph: who owns a service, who is on call, which changes caused incidents, what was decided after an outage. An agent calls six read-only company tools (`find_entities`, `service_profile`, `team_profile`, `person_profile`, `decisions_for`, `incident_history`), and the answer carries source chips for the nodes it relied on, a LLM / no-LLM badge, a grounded / no information badge, and a **Show queries** list with the exact Cypher and parameters of each call. A question the graph cannot answer comes back as "no information" rather than a guess. | `GRAPH.RO_QUERY` on the session world graph only (nothing a question does can write). Variable-length and optional matches over `OWNS`, `ON_CALL`, `MEMBER_OF`, `AUTHORED`, `CAUSED_BY`, `DECIDED`, `RECOMMENDS`. |

## Why a graph: three objections, answered

FalkorDB engineers will ask three things of any graph demo. Here is how the product answers each.

**"You planted the path."**
- *Judge mode* (`/judge`). The judge picks the target service and fault class. The injector
  publishes only `sha256(canonical fault JSON + nonce)`, and the agent investigates blind.
  The answer key sits in the API's SQLite store, which no tool, MCP handler or agent context
  can read. Reveal opens the commitment and compares the agent's root-cause id with it.
- *Decoys.* Every injection writes 3-6 timing-matched decoy changes through the same code
  path as the root, in seeded shuffled order. A leakage test
  (`api/tests/world/test_faults.py::test_leakage_single_feature_probes_stay_near_chance`)
  checks that no single non-symptom feature (delay, summary length, id rank, FalkorDB insertion
  order, event time, author/owner, kind) picks the root above chance.

**"This fits in a prompt."**
- The seeded world is 400 services across 3 regions with replicas: 858 service-region nodes,
  5,686 changes and 27,878 edges. Serialized for the full-context baseline, the *declared*
  catalog alone is about 23,800 prompt tokens per case, which fits in a 128k window. The catalog leaves out observed traffic, valid time, and record time, which the
  undeclared-coupling and late-arriving cases depend on.
- With `gpt-6-luna` reading the whole declared catalog, full context names the exact root in
  26/50 (10/20 held-out). The graph agent, using the same model, scores 50/50 (see
  [Live LLM run](benchmark.md#live-llm-run)).

**"Postgres with a recursive CTE looks identical."**
- The *Postgres twin* loads the same world into Postgres + pgvector and times the core graph
  operations on the same host with a warm cache. **On the 858-service world the differences
  are sub-millisecond and within run-to-run noise:** FalkorDB leads closure and shortest path
  (about 1.6-2.2x at p50), Postgres leads top-k vector search, and the closure ordering can
  flip between runs.
- On an 8,335-service world, FalkorDB's `algo.BFS` closure is **1.8-2.7x** faster and
  `shortestPath` is **10x** faster than the recursive CTEs, with identical results on all 50
  inputs.
- The bigger difference is design, not latency. Variable-length `WHERE` filters run after
  traversal (verified with `GRAPH.PROFILE`), so we materialize as-of snapshots with
  `GRAPH.COPY` in about 0.27 s for both views. Every proof then runs on a graph that holds only
  what was true, or known, at time T.

