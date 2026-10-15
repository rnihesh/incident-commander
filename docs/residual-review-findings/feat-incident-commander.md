# Residual review findings: feat/incident-commander

Deferred from the 10-reviewer code review (batches A, B, C applied), then resolved in a
follow-up pass. Kept as a record of what changed and what remains.

## Resolved
- `world/faults.py` split into the `ic.world.faults` package (common, training, heldout,
  commit_reveal, judge, storm); `__init__` re-exports the previous surface.
- `api/routes.py` split into `state.py`, `common.py`, `graph_view.py`, `maintenance.py`
  (784 -> 367 lines); `routes.py` re-exports the names other modules import.
- `actions/stub_remediation.py` renamed to `actions/pr_store.py`.
- Missing proof ref raises `ProofRefNotFound` (404); a real snapshot conflict on re-verify is a
  409 instead of an unhandled 500.
- `pr_graphs`, `pr_anchor`, `rehearsal_seal` tables are created at store init with migrations
  for older databases.
- MCP gained read-only `list_incidents` and `get_proof` (same payload as `/api/proof/{ref}`,
  no answer-key fields).
- `find_precedents` is offered to the LLM triage agent.
- Verdict summaries cut at `SUMMARY_MAX` end with "…" and carry `"truncated": true`.
- Per-key lock dicts (including the MCP snapshot-build locks) replaced by `ic.locks.KeyedLocks`,
  which evicts idle keys.
- Cancelled triage runs move OPEN/TRIAGING incidents to FAILED, always emit `error` then
  `done`, and free the session run slot.
- The seed-dependent MCP test fails instead of skipping.
- The bench runner's leak check counts only its own run ids, so a concurrent `ic bench` on
  the same FalkorDB no longer fails it.

## Remaining
- A run cancelled after its verdict keeps its state (the state machine has no edge from
  DIAGNOSED or remediation states to FAILED).
- Haiku 5.5's price in `llm_prices` is Haiku 4.5's list price until the official rate is confirmed.

### Found while capturing the wow-pack media (U10, Oct 10)
- **X-ray note vs plan (resolved after review).** The X-ray now also shows the EXPLAIN plan of
  a naive valid-time-filtered variable-length query on the session world, where `Filter` sits
  above `Conditional Variable Length Traverse`, next to the real query's plan on the snapshot.
  Funnel cuts use the newest run's last screening call. Proof X-rays still profile the
  `EXPLAINS` predicate, so different proofs can show the same tree.
- **Graph fit (resolved: refits once when the run settles and after a focus fades).** The canvas only refit on resize or new data: after a focus animation it
  stays zoomed on a node, and the storm graph first drew unfitted. Captures nudged the
  viewport to refit. A refit when the run settles would fix both.
- **Graph overlay.** The advisory banner ("Predicted by REC-034 ...") sits over the top of the
  canvas and can cover the topmost node label.
- **Learning-curve chart (resolved: labels are placed clear of markers, bars and the threshold).** The "÷2" labels overlapped the confidence markers at incidents 7-9.
- Graph labels: overlap is now avoided (priority alerting > suspect > path > symptomatic >
  rest; below, above, right, left, else skipped). Near-coincident nodes keep their positions,
  so two discs can still overlap; hover names either.

### Code review of the wow pack (Oct 11; 11 reviewers, verdict Ready with fixes)
All 6 actionable findings and 12 lower-confidence ones were fixed (`fix(review):` commits).
Remaining residual risks, recorded rather than fixed:
- `build_snapshots` gathers both views without cancelling the sibling on failure, so a
  straggler can create a `:verify:` graph after a challenge's cleanup; it is tracked and
  removed by session cleanup.
- SQLite challenge spend/refund run synchronously inside async handlers (existing store
  pattern; 5 s busy timeout).
- ~~`judge_challenges` rows are not pruned when sessions expire.~~ Resolved: the table is
  created by `Store.init` and session cleanup deletes an expired session's rows.
- Challenge `would_overturn` is always INCONCLUSIVE; after stacked tampers that make the
  challenged change the sole qualifier, a re-derive would diagnose that change instead.
- Fragility and X-ray rebuild missing as-of snapshots from the current (possibly tampered)
  world without saying so in the response.
- Snapshot graph names (which embed the session id) still appear in pre-existing run events;
  the new X-ray and fragility responses no longer return them.
- Benchmark payload repeats each refutation's query text per row (~56 KB); dedupe or gzip if
  the page gets slow.
- Cross-model adversarial review did not run (peer CLI auth unverifiable; tiny OpenAI budget);
  the in-process adversarial reviewer covered the lens. The separate validator batch was not
  run; every actionable fix landed with tests.

## Process
- Commits are dated from Oct 9 2026; hackathon window opens Oct 15. User reports the rules
  changed to allow this; confirm against the official rules before submitting.
