# MCP client setup

Incident Commander exposes its read-only graph tools (the same registry the agent uses) over MCP. `test_hypothesis` and every write path are not exposed. Two session-level tools sit beside them: `list_incidents` (id, title, ledger state, alert service and time of the incidents in the connection's world; never a root cause) and `get_proof` (the public `/api/proof/{ref}` page for a proof ref: verdict, hash-chained proofs, rehearsal, PR; it re-verifies nothing, and the ref alone is the credential). Each result carries the Cypher that produced it (`cypher`, `params`, `nodes_touched`, `ms`). The fragility map, the query X-ray and the judge challenge are console (UI) features only and are not MCP tools.

## Local (stdio): Claude Desktop, Cursor

FalkorDB must be running with `seed:world` loaded (`uv run ic seed`). Add to `claude_desktop_config.json` (Cursor: `.cursor/mcp.json`):

```json
{
  "mcpServers": {
    "incident-commander": {
      "command": "uv",
      "args": ["--directory", "/ABSOLUTE/PATH/TO/falkor/api", "run", "ic", "mcp"]
    }
  }
}
```

Default scope is read-only `seed:world`: `who_is_on_call`, `reflex` and the company tools (`find_entities`, `service_profile`, `team_profile`, `person_profile`, `decisions_for`, `incident_history`, the same ones the console's Ask panel uses) work. Snapshot tools (`list_symptoms`, `screen_candidates`, `causal_path`, `necessity`, `exonerate`, `verify_claim`, `find_precedents`, ...) need an incident, so bind a local session:

```json
"args": ["--directory", "/ABSOLUTE/PATH/TO/falkor/api", "run", "ic", "mcp",
         "--session", "<sid>", "--incident", "<iid>"]
```

Create one with `uv run ic inject --class bad_deploy --seed 3` (prints the session and incident ids). Missing as-of snapshots are built on demand for `--incident`, unless the session has a run in progress.

## Remote (streamable HTTP)

`POST /api/mcp-token` (session cookie required) returns a 1 h bearer token bound to that session. Point the client at `https://<api-host>/mcp` with `Authorization: Bearer <token>`. With a token, snapshot tools use that session's existing snapshots (open the incident in the console first). With no token only the `seed:world` tools answer; an invalid or expired token gets a 401. Requests are limited per client IP.

Snapshot tools that take no `incident_id` in the registry (`causal_path`, `impact_closure`, `observed_calls`, `rank_chokepoints`) accept an optional `incident_id` over MCP to pick the snapshot.

## Example prompts

- "Who is on call for svc-checkout@us-east right now?" (`who_is_on_call`; pass the epoch seconds of now)
- "Who owns checkout and who changed it recently?" (`find_entities` "checkout", then `service_profile` on the id it returns: owner, on call, recent changes with authors, incidents)
- "What did we decide after the last search outage?" (`incident_history` or `find_entities` for the incident, then `decisions_for` with its id: postmortem recommendations and ADRs)
- "What changed upstream of svc-checkout@us-east in the last 2 hours?" (`reflex` with `alert_service_id` svc-checkout@us-east, `t` now, `window_minutes` 120: search space and top recent suspects; with a session, `changes_in_window` for the incident)
- "Show the causal path from change chg-123 to svc-checkout@us-east." (session mode)
- "Which services are the biggest chokepoints?" (session mode, `rank_chokepoints`)
- "I think chg-123 caused inc-007. Check it." (session mode, `verify_claim` with `incident_id` and `entity_id`: VERIFIED when it qualifies and nothing else does, RIVAL when another candidate also qualifies, REFUTED with the first clause it fails and the query that decided it; read-only, writes no proof or ledger entry)
