# Proof-carrying incident response on FalkorDB

*How Incident Commander finds the change that broke production, proves that nothing else did,
and lets a judge break the proof on purpose.*

## The problem

At 3 AM an on-call engineer gets an alert that names a symptom ("checkout p99 latency"), not a
cause. Somewhere upstream, among hundreds of services and dozens of recent deploys, config
pushes and flag flips, one change did it. Today's AI SRE assistants answer with a ranked guess
and a paragraph of reasoning. You either trust it or redo the investigation yourself. None of
them tells you what they ruled out, and none lets you re-run the reasoning.

We built Incident Commander for the FalkorDB "Graph Hacks: Context for AI Agents" hackathon
around one idea: **every claim the agent makes is a stored query anyone can re-run**. The
verdict is a short chain of proofs, not a paragraph.

## Proof-carrying answers

The agent never writes Cypher. It picks a tool and a hypothesis kind: "this change qualifies",
"with it removed nothing else qualifies", "no other change upstream qualifies", "this decoy
fails". Before the falsifying query runs, the hypothesis is written to a per-session ledger
graph as `REGISTERED`, along with the tool and arguments that would refute it. After the query
runs, a `Proof` node stores a hash of the canonical result plus the exact Cypher and
parameters, linked with `PREV` to the previous proof. The head of that hash chain goes into
SQLite, outside the graph.

That gives the UI three views of one primitive:

- an **elimination funnel**, where every change in the window is cut by a named clause;
- an **absence certificate**, listing every upstream change that was checked and why it fails;
- a **Verify** button, which rebuilds the world and re-runs every proof.

Verify labels each proof VERIFIED, DRIFTED or FALSIFIED. It reports TAMPERED if someone edits
the ledger itself.

A verdict is only accepted when the three HOLDS proofs exist for the suspect. If two suspects
both survive, the answer is INCONCLUSIVE and no PR opens.

## The bitemporal snapshot trick

Root-cause analysis needs the graph *as it was* at alert time. A dependency added an hour after
the alert should not count. Neither should a deploy recorded late, or an on-call rotation that
changed overnight. Every dependency, ownership, on-call and change fact in our world carries
`valid_from`, `valid_to` and `recorded_at`.

The obvious approach is a variable-length match with a time filter on every relationship. Our
day-zero spike ran `GRAPH.PROFILE` on exactly that. The `Filter` sits *above* the
`Conditional Variable Length Traverse`, so the predicate is applied after traversal, not during
it. FalkorDB's `shortestPath` also does not accept relationship-property filters.

So we don't filter paths. We filter the graph. `GRAPH.COPY` cloned a 20k-node, 100k-edge graph
in about 67 ms in our spike, which makes a snapshot per investigation cheap. The snapshot builder:

1. copies the session world twice, into `:true` (valid at T) and `:known` (valid *and recorded*
   by T);
2. deletes everything outside the instant;
3. materializes one derived relationship that points the way failure travels.

```cypher
MATCH (c:Change)-[:CHANGES]->(s:Service) MERGE (c)-[:IMPACTS]->(s)
MATCH (a:Service)-[:DEPENDS_ON]->(b:Service) MERGE (b)-[:IMPACTS]->(a)
MATCH (a:Service)-[o:OBSERVED_CALL]->(b:Service)
WHERE o.count >= $thr AND o.first_seen <= $t MERGE (b)-[:IMPACTS]->(a)
```

*(from `api/src/ic/graph/snapshots.py:_materialize_impacts`)*

On the seed world both views build in parallel in about 270 ms, roughly 10k nodes and 34k
edges each. After that, every traversal is a plain `shortestPath`, `allShortestPaths`,
`algo.BFS` or `algo.SPpaths` over `IMPACTS`, with no time logic inside it.

The verdict uses the `:known` view, so the agent is judged on what it could have known. Records
written after the alert are flagged late-arriving. The same trick, one more copy away, gives
us a **multiverse rehearsal**: the remediation agent clones the `:true` snapshot, ends the
change's validity, restores its previous version, and re-checks every other candidate. It
refuses to open a PR if anything still explains part of the symptoms.

## The explanation predicate, and why reachability failed

Our first rule was the obvious one: the cause is a change upstream of the alerting service. It
did not survive the decoy test. To defend against "you planted the path", every injected fault
comes with 3-6 decoy changes, timing-matched and written by the same code path in shuffled
order. Many of them are upstream too. Reachability left several suspects standing in nearly
every case.

The fix was to explain *symptoms*, not the alert. The simulator writes a `Symptom` per affected
service with an onset time and a signature. A change qualifies only if it:

- happened before the earliest onset (**after_onset**);
- sits on a service that was among the first to fail (**origin**);
- reaches every symptomatic service (**coverage**);
- agrees with the onset order by hop distance (**order**);
- is a kind that can produce every observed signature, so a config push cannot cause TLS
  errors (**signature**).

The whole rule is one Cypher query per candidate, reading the symptoms from the snapshot itself:

```cypher
MATCH (c:Change {id: $entity_id})-[:CHANGES|EXPIRES_ON]->(home:Service)
MATCH (:Incident {id: $incident_id})-[:HAS]->(sym:Symptom)-[:ON]->(s:Service)
WITH c, home, sym, s, shortestPath((c)-[:IMPACTS*]->(s)) AS path
...
RETURN c.id AS id, CASE
  WHEN event_time >= earliest      THEN 'after_onset'
  WHEN NOT home_fails_first        THEN 'origin'
  WHEN size(unreached) > 0         THEN 'coverage'
  WHEN size(out_of_order) > 0      THEN 'order'
  WHEN size(bad_signatures) > 0    THEN 'signature'
END AS failed_clause
```

*(abridged from `api/src/ic/tools/predicate.py:EXPLAINS`)*

Because the query reads the graph rather than a cached answer, re-running it on an edited
world re-derives the verdict from scratch. That is what makes the judge mode work. A judge adds
one deploy on the root's service, a few minutes before onset, and presses Verify. The necessity
proof and the absence certificate flip to FALSIFIED, and the new change id is highlighted as
the responsible node.

Rehearsal needed one more clause. After a rollback, decoys on symptomatic callers of the root
look like independent causes on their own branch. The **independent** clause disqualifies a
candidate whose service the rolled-back change already explained and which started failing
after it: a downstream echo, not a second fault.

## Memory that rewrites topology

Some outages travel along calls nobody declared. The world has `OBSERVED_CALL` edges with
counts, and the snapshot turns busy ones into `IMPACTS`, so the agent can prove a path through
a hidden coupling. When such an incident resolves, the write-back records every
observed-but-undeclared hop on the causal path as a learned dependency, in one atomic statement:

```cypher
MATCH (a:Service {id: $caller}), (b:Service {id: $callee})
MERGE (a)-[l:LEARNED_DEPENDS_ON]->(b)
ON CREATE SET l.confidence = $initial, l.confirmations = 0, l.contradictions = 0, ...
ON MATCH SET l.confidence = CASE WHEN l.last_incident = $iid THEN l.confidence
                                 ELSE l.confidence + (1.0 - l.confidence) * $rate END, ...
```

*(abridged from `api/src/ic/memory/writeback.py:CONFIRM_Q`)*

On the next incident, `recall` reads those edges plus path-shape precedents and seeds the first
hypothesis. In our learning-curve run, six incidents rooted behind the same coupling went from
9 tool calls for the first to 5 for each of the rest. Confidence rose from 0.60 to 0.99. One
rule matters here: learned edges never become `IMPACTS`. Memory reorders the work, but every
verdict still has to carry its own proofs.

## Honest results

We ran 50 blind faults on seed 42: 30 from the training classes (bad deploy, config change, flag
flip) and 20 from held-out classes (expiry, undeclared coupling) written after the prompts and
tool descriptions froze.

Then we ran the same 50 cases with a real model, `gpt-6-luna`, behind every approach:

- The graph agent named the exact root entity in **50/50**, 20/20 on the held-out classes, with
  no fallback to the deterministic funnel.
- The same model given the whole declared catalog (about 24k tokens) got 26/50.
- Vector RAG over the same model, with `text-embedding-3-small`, got 4/50.

The graph agent does not need a big model either: `qwen3:8b` running locally on a laptop also
got 50/50, with no fallback, once we turned its thinking mode off. And a bigger model does not rescue the
full-context approach: `claude-opus-5-5` reading the whole catalog got 4 of the first 10 cases.

Same model, same cases, same candidate window. The only difference is whether it can query the
graph and has to prove what it claims. Without a model, the deterministic graph agent also gets
50/50; the heuristic stand-ins get 7/50 (vector) and 10/50 (full context).

Three caveats, stated as plainly as the numbers:

1. **One model, one run.** The live numbers are one run of `gpt-6-luna` at seed 42. LLM runs
   are not bit-reproducible, and a 10-case check with `claude-opus-5-5` did not close the full-context gap (4/10).
   The headline latency, scale and learning-curve numbers come from the no-LLM run.
2. **The 100% is partly by construction.** Each injected fault is checked against the same
   predicate the agent uses. The result shows the agent finds and proves the unique qualifying
   change among decoys. It does not show the predicate models real outages.
3. **The declared catalog fits in a prompt.** It is about 24k tokens for 858 service nodes. What
   does not fit cleanly is observed traffic, valid time and record time.

**Postgres.** We loaded the same world into Postgres with pgvector and recursive CTEs. On the
small world the comparison is mixed and noisy: in the published run FalkorDB wins closure
(1.11 vs 1.78 ms p50) and shortest path (0.79 vs 1.77 ms), Postgres wins top-5 vector search
(0.71 vs 1.32 ms), and an earlier run had Postgres ahead on closure. On an 8,335-service world,
`algo.BFS` closure is 1.8-2.7x faster and `shortestPath` is 10x faster (1.01 vs 10.16 ms), with
identical answers on all 50 inputs. Raw speed was not the deciding factor, though.
`GRAPH.COPY` made "the world at time T" a first-class graph, and that shaped the whole design.

## What we would do next

- **Real topology.** Import real topology from trace data instead of a generator inspired by
  published statistics.
- **LLM baselines.** Run the LLM-backed baselines with real token counts.
- **Richer proof status.** Replace commit statuses with GitHub check runs that render the proof
  chain in markdown.
- **Deferred features.** Min-cut minimal rollback and capacity-based "what saturates next".
  Both were deferred because `algo.maxFlow` returns no cut set.
- **Open data.** Publish the generator and leakage tests as a small benchmark, so other graph
  RCA systems can be measured on the same decoys.

The code, every query the product depends on, and the benchmark report are in the repository
README.
