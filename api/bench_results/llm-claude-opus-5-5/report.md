# Incident Commander benchmark

50 injected faults (30 training-class, 20 held-out), each in its own isolated copy of the seed world. A prediction is correct when it names the exact root-cause entity id from the answer key.

## Accuracy, time and tokens

| Approach | Accuracy (seen) | Accuracy (held-out) | Overall | Mean ms / case | LLM tokens / case | Prompt tokens / case (est.) | Tool calls / case |
|---|---|---|---|---|---|---|---|
| graph (llm: claude-opus-5-5) | 100% | 100% | 100% (50/50) | 12522 | 38334 | 0 | 6.3 |
| vector_rag (llm: claude-opus-5-5) | 17% | 15% | 16% (8/50) | 3756 | 1061 | 473 | 0.0 |
| full_context (llm: claude-opus-5-5) | 47% | 65% | 54% (27/50) | 6147 | 49617 | 23828 | 0.0 |

## Correct per fault class

| Approach | bad_deploy | config_change | flag_flip | expiry (held-out) | undeclared_coupling (held-out) |
|---|---|---|---|---|---|
| graph (llm: claude-opus-5-5) | 10/10 | 10/10 | 10/10 | 10/10 | 10/10 |
| vector_rag (llm: claude-opus-5-5) | 1/10 | 4/10 | 0/10 | 1/10 | 2/10 |
| full_context (llm: claude-opus-5-5) | 4/10 | 5/10 | 5/10 | 10/10 | 3/10 |

## Per-step latency: FalkorDB vs Postgres

_Not measured: skipped._

## Scale: FalkorDB vs Postgres on a large world

_Not yet measured (run `ic bench --scale N`)._

## Learning curve

_Not yet measured (run `ic learn`)._

## Notes

- Correct = exact root entity id (change or expiry) from the answer key; an INCONCLUSIVE graph verdict makes no claim and counts as not correct.
- Held-out classes (expiry, undeclared_coupling) were written after the triage prompts and tool descriptions, which do not change in this unit and never name them as fault classes (tested; Expiry as a graph label and the observed_calls tool predate them).
- Every approach reads the same as-of snapshot (known view at alert time) and the same candidate window [earliest onset - 120 min, alert time].
- full_context serializes the declared service catalog (DEPENDS_ON only, no OBSERVED_CALL); prompt tokens are estimated as len(text)/4 against a 128,000-token limit.
- Mean ms is wall time per case for the approach alone (world copy, injection and snapshot builds excluded).

---

Seed 42 · case seeds 53b6b393b51d (full list in report.json) · commit `0ef6f6c9ba26714bd6ed10714c0cfaec413bbb0b` · 50 cases · LLM: claude-opus-5-5
