# Using Incident Commander

A walkthrough of every page and integration. Setup is in the [README](../README.md#quickstart).

**Console (`/`).** A first visit opens on a welcome screen: what the product does, how it works
in three steps, a fault choice (bad deploy, config change, feature flag flip) and **Start an
incident**, plus links to Judge mode, **Watch a sample run** (the recorded `gpt-6-luna` hero run,
with a playback bar and a "Sample run (recording)" banner; also at `/?sample=1`) and **Fire an
alert storm**. Nothing plays on its own. A live run streams over SSE:

- first-guess strip (before the snapshot, later marked replaced with a suspects diff)
- graph canvas with the causal path
- elimination funnel (each cut expands to its query, with **X-ray** on live runs)
- verdict
- absence certificate (clause bars, clause chips, onset strip)
- hypothesis chain (each proof has **Show query**, **X-ray** and **Re-run**)
- response panel (handoff, rehearsal, PR, status, page, memory)
- learning curve (the committed `learning_curve.json`)

**Ask the graph.** Press **Ask the graph** (bottom right of the console) to open the chat panel
and type a question, or click a starter question: "Who owns checkout and who is on call right now?", "Which changes caused incidents
last week?", "What did we decide after the last config-service outage?", "Who changed the rate
limiter recently?". The answer lists its sources and **Show queries** reveals the Cypher behind
each claim. A question outside the graph (the weather, say) answers "no information". With
an LLM key set the agent plans the lookups; without one the panel runs in a limited no-LLM
mode (also used if the model fails or the daily spend cap is reached) where a deterministic
router answers a fixed set of question shapes (owner, on call, recent changes, incidents,
decisions) and the response says so. With a live incident open, the chat also answers
questions about it ("Why did you blame chg-...?", "What got ruled out?") from the incident's
stored proofs through the read-only `incident_proofs` tool. The conversation stays in the tab
across runs. Each session
may ask 20 questions per hour (`IC_SESSION_ASKS_PER_HOUR`); an incident is not needed, and
nothing you ask writes to the graph.

**Verify chain** re-runs every proof against the current world. The drawer's
**Was it my deploy?** tab is the judge challenge: pick any change the investigation eliminated
and `verify_claim` re-checks it on a fresh as-of build of the session world, answering
VERIFIED, RIVAL or REFUTED with the failing clause and the query that decided it. Two
challenges per incident (a third is 429); re-derive resets the count. The **Scenario** menu (`?scenario=hero|inconclusive|refused|degraded|recorded|ratelimited|expired`)
plays hand-built UI fixtures for states that are hard to trigger on demand. These fixtures use
placeholder hashes and are not live runs.

**Judge mode (`/judge`).**

1. Search for a target service. The picker shows ALERT PATH OK or NO ALERT PATH.
2. Choose a fault class and press **Commit and inject blind**. You get only the commitment
   hash. The run streams with no fault label.
3. **Reveal** opens after the verdict or 120 s. It shows the preimage, the recomputed hash,
   and whether the agent's root-cause id matches.
4. **Tamper.** **Find edits that flip the verdict** fills "Try these" with the single edits
   (at most 10, each tried on its own snapshot copy) that would break this verdict, and the
   clause each one breaks; clicking one pre-fills the form. Or choose **Add deploy upstream**,
   **Remove edge** or **Change timestamp** yourself. **Review edit**, then **Confirm and
   write**. The exact Cypher and parameters are shown.
5. **Verify.** The affected proofs turn FALSIFIED, with the responsible node named and
   highlighted. Untick "Recorded before the alert" to see the same flip marked late-arriving.
6. **Re-derive** starts a new investigation on the tampered world.

For FalkorDB Browser tampering on a local machine, see
[docs/notes/browser-tamper.md](notes/browser-tamper.md). Editing a `Proof` in the ledger
graph makes Verify report TAMPERED, because the chain head no longer matches the SQLite anchor.

**Replay (`/replay/{incident}`).** Linked from the console after a live run. Scrub through
changes landing and services turning symptomatic. Toggle **as-happened** (late records shown)
vs **as-known** (late records hidden). The root is not marked.

**Storm.** Press **Storm** in the console. 2-3 independent faults (one may be a joint
flag+config fault) fire 200-400 pages. The panel shows intake, WCC components, the minimal
explaining set per incident (with "jointly caused" when it applies) and seeded-betweenness
chokepoints.

**Proof page (`/proof/{ref}`).** The rollback PR's body and its `incident-commander/proofs`
commit status link here. `ref` is a random 24-hex id minted when the PR copies were made; the
page needs no session and never shows the session id or the answer key. It reads the PR's own
copies (`pr:{sid}:{iid}:ledger` / `:world`), not the live session: title, alert service,
verdict, suspect, chain head, anchor, rehearsal result, PR link, and the hypothesis chain with
**Show query** / **Copy query** per proof. **Re-verify** (`POST /api/proof/{ref}/verify`)
replays every proof on fresh snapshots of the PR's world copy
(`pr:{sid}:{iid}:verify:true|known`, deleted afterwards) and shows VERIFIED, DRIFTED,
FALSIFIED, TAMPERED or UNANCHORED. Editing the session world after the PR opened does not
change this result. It also refreshes the commit status: for real on a real PR within the
hourly action budget, simulated otherwise. Re-verify is capped per IP
(`IC_PROOF_VERIFIES_PER_IP_HOUR`, default 12) and debounced per proof
(`IC_PROOF_RERUN_INTERVAL_S`, default 60 s). Inside that window the last report comes back
marked "cached". PRs auto-close after an hour, and their proof page goes with them (404).

**Benchmark (`/benchmark`).** Renders `api/bench_results/report.json` (served by
`GET /api/benchmark`): the accuracy, per-class, latency and scale tables, the learning and
forgetting curve, the live LLM runs and, per run, "Why it was wrong" with every wrong
prediction's failing clause and deciding query.
The page only renders results. Benchmarks run locally from the CLI.

**MCP.** The same registry tools are served over MCP, read-only: `test_hypothesis`, rehearsal,
storm and anything that writes are excluded. `verify_claim` is included, so an external agent
can check a root cause it has in mind and get VERIFIED, RIVAL or REFUTED with the clause and
query. Two read-only session tools come with them:
`list_incidents` (public fields and ledger state, never a root cause) and `get_proof` (the
public proof page for a proof ref). The six company tools from Ask the graph (`find_entities`,
`service_profile`, `team_profile`, `person_profile`, `decisions_for`, `incident_history`) are
exposed over MCP too, read-only and with no incident needed, so Claude Desktop or Cursor can ask
who owns a service or who is on call. For Claude Desktop
(`claude_desktop_config.json`) or Cursor (`.cursor/mcp.json`):

```json
{
  "mcpServers": {
    "incident-commander": {
      "command": "uv",
      "args": ["--directory", "/ABSOLUTE/PATH/TO/incident-commander/api", "run", "ic", "mcp"]
    }
  }
}
```

The default scope is `seed:world` (`who_is_on_call`, `reflex`). To use the snapshot tools,
first create a local incident with `uv run ic inject --class bad_deploy --seed 3`, then add
`"--session", "<sid>", "--incident", "<iid>"` to `args`. Over HTTP, `POST /api/mcp-token`
returns a 1-hour bearer token bound to your session, for `http(s)://<api>/mcp`. Details are in
[docs/notes/mcp-client-setup.md](notes/mcp-client-setup.md).
