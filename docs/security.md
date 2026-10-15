# Security, abuse controls and determinism

## Security and abuse controls

- **Session isolation.** Session ids are 128-bit, server-generated, validated on every request
  and carried in an httpOnly, SameSite=Lax cookie (Secure by default). Each session has its own
  graphs. Graph names come from one validated module, and the registry refuses any graph
  outside the caller's scope. The LLM picks tool names and domain arguments only: argument
  names like `graph` or `sid` are rejected from schemas, and it never writes Cypher.
- **Limits** (`IC_*`). At most 8 concurrent sessions. Per session: 3 runs per hour, 1
  concurrent. Per IP: 2 concurrent sessions, 6 new sessions per hour, 6 runs per hour. Global:
  60 runs per hour. These are SQLite-backed, so they survive restarts. Over a limit, the
  visitor gets the recorded run.
- **Spend breaker.** A daily dollar cap (default $5), computed from logged token usage. When
  it trips, visitors get recorded runs.
- **Action budget.** At most 20 PRs and pages per hour. Over budget, actions run in simulated
  mode and are labelled.
- **Verify before act.** Remediation re-verifies the chain from the ledger and refuses if it
  does not hold. It also refuses when the rehearsal leaves any symptom explained.
- **No public FalkorDB ports.** The dev compose profile binds FalkorDB and Browser to
  `127.0.0.1` only. The `prod` profile (`falkordb-prod`, `api`, `caddy`) publishes only Caddy's
  80/443. FalkorDB has no host ports, its Browser is off, and it sits on an internal-only
  network. The `default` user is disabled. The API logs in as an `ic` ACL user that can only
  reach `seed:*`, `s:*`, `pr:*` and `b:*` keys and the `GRAPH.*` commands it uses, granted by
  name. See [docs/notes/deploy.md](notes/deploy.md).
- **Public proof page.** It is reached by an unguessable ref, never by session id. Responses
  are built field by field and scrubbed of the sid. Re-verify reads only the PR copies, is
  rate-limited per IP and debounced per ref, and runs one at a time.
- **PR auto-close.** Every 10 minutes the cleanup loop closes `ic/rollback-*` PRs older than one
  hour, but only on the configured repo's own branches, never forks. In the same pass it
  deletes those PRs' `pr:` graphs, plus any PR copies past the one-hour TTL.
- **GitHub and Discord writes.** Graph-derived names are validated by regex before they become
  branch names or file paths. Visitor- or LLM-originated text is put in code spans with `@`
  neutralized and truncated. Discord `allowed_mentions` is disabled.
- **Answer-key isolation.** Ground truth exists only in SQLite and is never written to a graph.
  Judge runs start with no fault label, so neither the agent context nor the event stream
  carries it. Only `reveal` reads it.
- **Also:** state-changing routes require `Content-Type: application/json`. CORS allows
  exactly one origin. Alert webhooks need an HMAC signature. Tamper writes are parameterized,
  limited to the caller's own world, and refused (409) while a run is active. MCP over HTTP
  needs a session-bound bearer token for anything beyond `seed:world`.

## Determinism

- The world is fully determined by its seed (`random.Random(seed)` only). `ic seed --seed 42`
  prints a content hash:
  `879505a4501940838d15ac0743f6891686655da34e39d24e7521e99b577352ae`. CI re-seeds on a fresh
  FalkorDB on every push and fails if the hash differs from this one.
- The world clock is frozen at `WORLD_NOW` (2026-10-01T00:00:00Z).
- `algo.betweenness` runs with `samplingSeed: 42`. Canonical paths are the lexicographically
  smallest of the equal-length shortest paths, so proof hashes do not depend on `algo.SPpaths`
  tie order.
- Fault injection takes `seed` (`/api/inject`, `/api/judge/inject`, `ic inject --seed`), and
  layout positions are precomputed.
- With no LLM key the agent is deterministic end to end. With a key, LLM runs are not
  bit-reproducible (temperature 0 where the model accepts it, but providers do not promise
  determinism); the proofs they produce are re-runnable all the same. `agent/transcripts.py` provides an
  LLM record/replay layer keyed by request hash. It is used in tests and is not wired into the
  API.
- **Video.** Takes are live runs on seed 42 (the gpt-6-luna agent when a key is set, else the deterministic agent). Any shot taken from a
  `?scenario=` fixture is labelled as such in the video description.
