# Spike notes (throwaway experiments, code deleted)

Image: `falkordb/falkordb@sha256:107455e862a8…` (pinned in `docker-compose.yml`). Local FalkorDB runs on `localhost:16379` (Browser `localhost:13000`) because a host Redis already owns 6379.

## S1 — GRAPH.COPY and memory

- Graph of 20k nodes / 100k edges with a range index: `GRAPH.MEMORY USAGE` ≈ 10 MB total (indices 4 MB, relation matrices 2 MB).
- `GRAPH.COPY` (client `g.copy(name)`): **~67 ms** per copy, stable over 3 runs.
- Conclusion: KTD2 primary path holds. 5 copies per investigation ≈ 0.35 s. No fallback needed.

## S2 — variable-length WHERE pruning

`GRAPH.PROFILE` of `MATCH p=(a)-[*1..4]->(b) WHERE all(r IN relationships(p) WHERE r.vf < 50)` shows `Filter` **above** `Conditional Variable Length Traverse`: the predicate is applied after traversal, not during. Confirms the materialized as-of snapshot design.

## S3 — algorithm signatures (all verified)

- `CALL algo.BFS(startNode, maxDepth, 'REL') YIELD nodes, edges`
- `CALL algo.SPpaths({sourceNode, targetNode, relTypes:[..], maxLen, relDirection:'outgoing', pathCount}) YIELD path, pathWeight`
- `CALL algo.SSpaths({sourceNode, relTypes:[..], maxLen, relDirection, pathCount}) YIELD path`
- `CALL algo.WCC({nodeLabels:[..], relationshipTypes:[..]}) YIELD node, componentId`
- `CALL algo.betweenness({nodeLabels, relationshipTypes, samplingSize, samplingSeed}) YIELD node, score`
- `CALL algo.pageRank('Label', 'REL') YIELD node, score`
- `shortestPath((a)-[:REL*]->(b))` works with bound endpoints.

## S4 — vector and full-text

- `db.idx.vector.queryNodes` score is a **distance**: lower = closer (cosine: 0.006 for near, 0.89 for far).
- Full-text fuzzy `%term%` matches edit distance 1 (`%timout%` → "timeout", `%certifcate%` → "certificate").

## Pending

- S5 (ACL judge user + Browser), S6 (model ids / budget), S7 (FastMCP mount), S8 (PAT scope) — checked when their units start.

## FalkorDB quirks found while building U4

- `shortestPath` is only allowed in `WITH`/`RETURN`; `allShortestPaths` needs endpoints bound by a preceding `WITH`.
- A variable-length relationship variable is a Path; use `relationships(p)`.
- `exists(pattern)` is unsupported; use a pattern comprehension.
- `algo.BFS` excludes the start node.
- `UNWIND` of a collected node list followed by `MATCH` on an indexed property returned no rows; rewritten as a `*0..3` path.
