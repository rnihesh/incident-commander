# Incident Commander benchmark

5 injected faults (3 training-class, 2 held-out), each in its own isolated copy of the seed world. A prediction is correct when it names the exact root-cause entity id from the answer key.

## Accuracy, time and tokens

| Approach | Accuracy (seen) | Accuracy (held-out) | Overall | Mean ms / case | LLM tokens / case | Prompt tokens / case (est.) | Tool calls / case |
|---|---|---|---|---|---|---|---|
| graph (llm: gpt-6-luna) | 100% | 100% | 100% (5/5) | 12080 | 22618 | 0 | 7.0 |

## Correct per fault class

| Approach | bad_deploy | config_change | flag_flip | expiry (held-out) | undeclared_coupling (held-out) |
|---|---|---|---|---|---|
| graph (llm: gpt-6-luna) | 1/1 | 1/1 | 1/1 | 1/1 | 1/1 |

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

Seed 42 · case seeds e711856546a4 (full list in report.json) · commit `76921df63c47e08bf9a8422b487081bca1d7180c` · 5 cases · LLM: gpt-6-luna
