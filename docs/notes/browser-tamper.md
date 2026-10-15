# Tampering through FalkorDB Browser (local and video only)

Use this on a local machine or in a recording. The public deploy never exposes FalkorDB or the
bundled Browser (KTD18). There, judges tamper through the in-app panel at `/judge`, which writes
only to their own `s:{sid}:world` using parameterized Cypher (KTD11, R27). Raw Browser access to a
shared instance would show every session's graph names and allow writes outside the judge's own
world (see the ACL findings below).

## 1. Open the Browser

The dev compose profile publishes FalkorDB on `127.0.0.1:16379` and the bundled Browser on
`127.0.0.1:13000`:

```bash
docker compose --profile dev up -d falkordb
open http://localhost:13000
```

Log in with **Manual configuration**. The Browser runs inside the container, so the host is the
container's own Redis:

| Field    | Value                                                      |
|----------|------------------------------------------------------------|
| Host     | `localhost`                                                |
| Port     | `6379` (inside the container, not the published 16379)     |
| Username | `default` (no password on the dev profile), or the judge user from section 3 |
| TLS      | off                                                        |

From the host (redis-cli, scripts) the same server is `falkor://localhost:16379`.

## 2. Find your session world and add a qualifying deploy upstream

1. Run a judge round at `http://localhost:3001/judge` and let it reach its verdict.
2. Your session id is the `ic_sid` cookie (DevTools, Application, Cookies). Your world is
   `s:<ic_sid>:world`. Select it in the Browser's graph picker.
3. Read the incident's alert time `t`, earliest onset and first symptomatic service. The root
   cause is a change on the service with the earliest onset, so a deploy there just before the
   onset qualifies:

```cypher
MATCH (i:Incident {id: 'inc-041'})-[:HAS]->(y:Symptom)
RETURN i.opened_at AS t, y.service_id AS service, y.onset AS onset
ORDER BY y.onset LIMIT 1
```

4. Add a deploy on that service, effective before the onset and recorded before `t` (so the
   as-known view sees it too). Use the next free `chg-NNNNN` id: later injections number their
   changes from the highest one, and a non-numeric suffix breaks that.

```cypher
MATCH (c:Change) WITH max(toInteger(substring(c.id, 4))) + 1 AS n
MATCH (s:Service {id: 'svc-orders@us-east'})
CREATE (d:Change {
  id: 'chg-' + right('0000' + toString(n), 5),
  kind: 'deploy', service_id: s.id,
  summary: 'Deploy v9.9.9: manual hotfix rollout',
  valid_from: 1790830000,      // onset minus a few minutes
  valid_to: 4102444800,        // OPEN_END: still in effect
  recorded_at: 1790830060,     // <= t: known at alert time; > t: late-arriving
  value: 'v9.9.9', prev_value: 'v9.9.8'
})-[:CHANGES]->(s)
RETURN d.id
```

5. Back in the console, press **Verify**. The absence certificate (and the necessity proof) flip
   to **FALSIFIED**, and the new change id is listed as the responsible node (AE5). Set
   `recorded_at` after `t` to see the same flip marked *late-arriving*. **Re-derive** in judge
   mode starts a new run on the same incident and alert time, against the tampered world.

Other edits to try:

```cypher
// remove a dependency edge (the caller no longer depends on the callee)
MATCH (:Service {id: 'svc-checkout@us-east'})-[e:DEPENDS_ON]->(:Service {id: 'svc-orders@us-east'})
DELETE e

// move a change after the earliest onset: it stops qualifying
MATCH (c:Change {id: 'chg-05702'}) SET c.valid_from = 1790830500
```

Editing the ledger graph (`s:<sid>:ledger`), for example changing a `Proof.result_hash`, makes
Verify report **TAMPERED**: the chain head no longer matches the anchor held in the API's SQLite
store.

## 3. S5: a judge ACL user restricted to session graphs

Recipe (run as an admin on the local container). The FalkorDB `GRAPH.*` commands are not in any
`@read` / `@write` ACL category, so they have to be granted by name:

```bash
redis-cli -p 16379 ACL SETUSER judge on '>choose-a-password' \
  resetkeys '~s:<ic_sid>:*' resetchannels -@all \
  +GRAPH.QUERY +GRAPH.RO_QUERY +GRAPH.EXPLAIN +GRAPH.PROFILE +GRAPH.LIST +GRAPH.CONFIG \
  +PING +HELLO +AUTH +CLIENT +INFO +COMMAND +SELECT +ECHO
# afterwards
redis-cli -p 16379 ACL DELUSER judge
```

`~s:*` (every session) also works, but then the judge can read and write every visitor's world
and ledger. Scope the key pattern to one session: `~s:<ic_sid>:*`.

### Findings (verified 2026-10-09 against the pinned image `falkordb/falkordb@sha256:107455e8...`)

Tested with a throwaway user scoped to one probe session prefix, over redis-cli and through the
Browser at `http://localhost:13000` (host `localhost`, port `6379`). The user and probe graph
were deleted afterwards; `seed:world` was never written.

- **Key patterns are enforced on graph commands.** `GRAPH.QUERY` / `GRAPH.RO_QUERY` on the
  user's own `s:<sid>:world` succeed (reads and writes). On `seed:world` or another session's
  `s:<other>:world` they fail with `NOPERM No permissions to access a key`, for reads and writes
  alike.
- **Browser login works with the command list above.** The header shows the user, the graph
  picker lists graphs, selecting the session world loads its schema panel (labels, relationship
  counts), and a `SET` typed into the editor and run with Enter wrote the change. Selecting
  `seed:world` shows the same `NOPERM` error toast. The only other noise is a harmless "Failed
  to load demo graphs" toast (the guided-tour loader).
- **`GRAPH.LIST` is not filtered by the key pattern.** The picker shows every graph name on the
  server (other sessions' `s:<sid>:*` and `pr:<sid>:*`), so session ids leak, though not their
  contents. Dropping `+GRAPH.LIST` hides the list, but then the Browser's picker is empty and the
  judge has to open their graph by URL
  (`/graph?graph=s%3A<sid>%3Aworld`). Acceptable only on a local or recording machine. This is
  one more reason the public deploy keeps the Browser off (KTD18).
- `GRAPH.DELETE`, `GRAPH.COPY`, `KEYS` and other admin commands are refused (not granted), so the
  judge cannot drop or clone graphs.
- The dev profile's `default` user is `nopass ~* +@all`. Anyone who can reach port 16379 or the
  Browser on this machine can edit everything, including `seed:world`. The ports are bound to
  127.0.0.1 for that reason. KTD18's prod settings (`requirepass`, default user disabled, no
  published ports, Browser off) are not exercised here.
