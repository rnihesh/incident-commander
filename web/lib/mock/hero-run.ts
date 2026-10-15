import type { RunEvent } from "../events";
import type {
  ActionData,
  CertificateData,
  Clause,
  EliminatedChange,
  FunnelData,
  ProofData,
  RunStartedData,
} from "../payloads";
import { EventBuilder, T0, fakeHash } from "./builders";
import {
  HERO_ALERTING,
  HERO_LATE,
  HERO_PATH,
  HERO_SERVICE_IDS,
  HERO_SYMPTOMATIC,
  serviceId,
} from "./hero-graph";

export const HERO_RUN_ID = "run_7f3a91";
export const HERO_SESSION = "7f3a";
export const HERO_ROOT_CHANGE = "chg-0418";
const G = (k: "true" | "known") => `s:${HERO_SESSION}:${k}`;

// ---- the 312 changes in the window ----------------------------------------
function mulberry32(seed: number) {
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

const pad = (n: number) => `chg-${String(n).padStart(4, "0")}`;

/** Named rivals with a fixed story; the rest are generated deterministically. */
const SPECIAL: EliminatedChange[] = [
  { change: "chg-0391", service: serviceId("config-store"), clause: "after_onset" },
  { change: "chg-0411", service: serviceId("feature-flags"), clause: "order" },
  { change: "chg-0397", service: serviceId("tax"), clause: "coverage" },
  { change: "chg-0421", service: HERO_LATE[0], clause: "coverage", late: true },
];

export function buildRivals(): EliminatedChange[] {
  const rnd = mulberry32(418);
  const quota: Record<Clause, number> = {
    after_onset: 271 - 1,
    origin: 32,
    coverage: 7 - 2,
    order: 0,
    signature: 0,
  };
  const used = new Set<string>([...SPECIAL.map((s) => s.change), "chg-0418"]);
  const reserved = new Set(HERO_PATH); // path services only carry the suspect's own change
  const pool = HERO_SERVICE_IDS.filter((id) => !reserved.has(id));
  const out: EliminatedChange[] = [...SPECIAL];
  let n = 100;
  (Object.keys(quota) as Clause[]).forEach((clause) => {
    for (let i = 0; i < quota[clause]; i++) {
      while (used.has(pad(n))) n++;
      used.add(pad(n));
      // coverage near-misses sit on plausible upstream services, never the alerting one
      const svc = pool[Math.floor(rnd() * pool.length)];
      out.push({ change: pad(n), service: svc, clause });
      n += 1 + Math.floor(rnd() * 2);
    }
  });
  return out;
}

const CLAUSE_LABEL: Record<Clause, string> = {
  after_onset: "Precedes earliest onset",
  origin: "Origin can reach the alerting service",
  coverage: "Reach covers every symptomatic service",
  order: "Onset order matches hop order",
  signature: "Change type fits the symptom signatures",
};

const CYPHER: Record<Clause, string> = {
  after_onset: `MATCH (c:Change)
WHERE c.valid_from >= $win_from AND c.valid_from <= $win_to
  AND c.valid_from < $earliest_onset
RETURN collect(c.id) AS survivors`,
  origin: `UNWIND $cands AS cid
MATCH (c:Change {id: cid})-[:APPLIED_TO]->(o:Service)
CALL algo.BFS(o, 6, 'IMPACTS') YIELD nodes
WITH c, [n IN nodes | n.id] AS reach
WHERE $alert_svc IN reach
RETURN collect(c.id) AS survivors`,
  coverage: `UNWIND $cands AS cid
MATCH (c:Change {id: cid})-[:APPLIED_TO]->(o:Service)
UNWIND $symptomatic AS sid
MATCH (s:Service {id: sid})
CALL algo.SPpaths({sourceNode: o, targetNode: s, relTypes: ['IMPACTS'],
                   maxLen: 6, relDirection: 'outgoing', pathCount: 1}) YIELD path
WITH c, count(DISTINCT s) AS covered
WHERE covered = size($symptomatic)
RETURN collect(c.id) AS survivors`,
  order: `UNWIND $cands AS cid
MATCH (c:Change {id: cid})-[:APPLIED_TO]->(o:Service)
MATCH (sym:Symptom)-[:OBSERVED_ON]->(s:Service)
CALL algo.SPpaths({sourceNode: o, targetNode: s, relTypes: ['IMPACTS'],
                   maxLen: 6, relDirection: 'outgoing', pathCount: 1}) YIELD path
WITH c, collect({hops: length(path), onset: sym.onset}) AS pairs
WHERE ic.monotone(pairs)
RETURN collect(c.id) AS survivors`,
  signature: `UNWIND $cands AS cid
MATCH (c:Change {id: cid})-[:OF_TYPE]->(t:ChangeType)
MATCH (sym:Symptom)
WITH c, t, collect(DISTINCT sym.signature) AS sigs
WHERE all(x IN sigs WHERE x IN t.compatible_signatures)
RETURN collect(c.id) AS survivors`,
};

const COUNTS: [Clause, number, number][] = [
  ["after_onset", 312, 41],
  ["origin", 41, 9],
  ["coverage", 9, 2],
  ["order", 2, 1],
  ["signature", 1, 1],
];

const TOP_AFTER_FIRST = ["chg-0418", "chg-0411", "chg-0397"]; // reflex said 0391, not 0397

// ---- builders ---------------------------------------------------------------
function tool(
  b: EventBuilder,
  at: number,
  agent: "system" | "triage",
  name: string,
  graph: string,
  cypher: string[],
  params: Record<string, unknown>[],
  touched: string[],
  ms: number,
  result: unknown,
) {
  b.add("tool_call", agent, at, {
    tool: name,
    graph,
    cypher,
    params,
    nodes_touched: touched,
    ms,
    result,
  });
}

export function buildHeroEvents(): RunEvent[] {
  const b = new EventBuilder(HERO_RUN_ID, T0);
  const rivals = buildRivals();
  const win = { from: "2026-10-09T11:36:41Z", to: "2026-10-09T14:06:41Z" };
  const onset = "2026-10-09T12:07:40Z";
  const pricing = serviceId("pricing");
  const checkout = HERO_ALERTING;

  const started: RunStartedData = {
    incident_id: "inc-20261009-0071",
    title: "svc-checkout@us-east: p99 latency and 5xx spike",
    scenario: "hero",
    alert: {
      service: checkout,
      signal: "p99 latency 4.8 s, 5xx 11.2%",
      severity: "SEV-2",
      fired_at: "2026-10-09T14:06:41Z",
      detail: "Latency crept from 12:07Z; 5xx crossed the SLO at 14:06Z.",
    },
  };
  b.add("run_started", "system", 0, started);
  b.add("reflex", "system", 90, {
    ms: 84,
    label: "pre-snapshot",
    blast_radius: 37,
    suspects: [
      { change: "chg-0418", service: pricing, summary: "deploy pricing@v42" },
      { change: "chg-0411", service: serviceId("feature-flags"), summary: "flag flip checkout.bundle_pricing" },
      { change: "chg-0391", service: serviceId("config-store"), summary: "config ttl_seconds 300 to 30" },
    ],
    oncall: { name: "Priya Raman", team: "Checkout Platform", channel: "#oncall-checkout" },
  });
  b.add("state", "system", 150, { state: "TRIAGING" });
  b.add("narration", "triage", 900, {
    text: "Alert on svc-checkout@us-east. Rebuilding the graph as of 14:06:41Z, once as it was true and once as it was known.",
  });

  // snapshot
  tool(b, 1500, "triage", "create_snapshot", G("true"),
    [
      `CALL ic.snapshot({world: $world, as_of: $t, view: 'true', target: $target})`,
      `CALL ic.snapshot({world: $world, as_of: $t, view: 'known', target: $target})`,
    ],
    [{ world: `s:${HERO_SESSION}:world`, t: "2026-10-09T14:06:41Z", target: G("true") }, { target: G("known") }],
    [], 138, { nodes: 4318, edges: 11902 });
  b.add("snapshot", "system", 1620, {
    build_ms: 140,
    graphs: [
      { name: "true", nodes: 4318, edges: 11902 },
      { name: "known", nodes: 4316, edges: 11897 },
    ],
    symptomatic: HERO_SYMPTOMATIC,
    late_arriving: HERO_LATE,
  });

  tool(b, 2300, "triage", "get_symptoms", G("true"),
    [`MATCH (sym:Symptom)-[:OBSERVED_ON]->(s:Service)
RETURN s.id AS service, sym.onset AS onset, sym.signature AS signature
ORDER BY sym.onset`],
    [{}],
    HERO_SYMPTOMATIC, 9,
    { earliest_onset: onset, symptomatic: HERO_SYMPTOMATIC.length, signatures: ["latency", "timeout", "error_5xx"] });

  tool(b, 3200, "triage", "list_changes_in_window", G("true"),
    [`MATCH (c:Change)-[:APPLIED_TO]->(s:Service)
WHERE c.valid_from >= $win_from AND c.valid_from <= $win_to
RETURN c.id, c.kind, s.id, c.valid_from
ORDER BY c.valid_from`],
    [{ win_from: win.from, win_to: win.to }],
    ["chg-0418", "chg-0411", "chg-0397", "chg-0391"], 14, { count: 312 });

  // funnel: one tool call then one funnel event per clause
  let survivors = rivals.length + 1;
  const cuts: Record<Clause, EliminatedChange[]> = {
    after_onset: [], origin: [], coverage: [], order: [], signature: [],
  };
  rivals.forEach((r) => cuts[r.clause].push(r));
  let at = 4300;
  COUNTS.forEach(([clause, before, after], idx) => {
    const ms = [11, 24, 61, 18, 7][idx];
    const params: Record<string, unknown> =
      clause === "after_onset"
        ? { win_from: win.from, win_to: win.to, earliest_onset: onset }
        : clause === "origin"
          ? { cands: `[${before} change ids]`, alert_svc: checkout }
          : clause === "signature"
            ? { cands: [HERO_ROOT_CHANGE] }
            : { cands: `[${before} change ids]`, symptomatic: HERO_SYMPTOMATIC.length };
    tool(b, at, "triage", `cut_${clause}`, G("true"), [CYPHER[clause]], [params],
      cuts[clause].slice(0, 6).map((c) => c.change), ms, { before, after });
    survivors = after;
    const data: FunnelData = {
      step: idx + 1,
      clause,
      label: CLAUSE_LABEL[clause],
      before,
      after: survivors,
      cypher: CYPHER[clause],
      params,
      eliminated: cuts[clause],
      top_suspects: clause === "after_onset" ? TOP_AFTER_FIRST : ["chg-0418"],
    };
    b.add("funnel", "triage", at + 400, data);
    at += 1700;
  });

  // hypotheses and proofs
  const proofs: { id: string; suspect: string; statement: string; tool: string; expected: string; state: "HOLDS" | "REFUTED"; summary: string; query: string; params: Record<string, unknown> }[] = [
    {
      id: "hyp-1", suspect: "chg-0418",
      statement: "chg-0418 (deploy pricing@v42) satisfies all four clauses against every symptom.",
      tool: "explain_symptoms", expected: "qualifies = true",
      state: "HOLDS",
      summary: "Qualifies: precedes onset by 4 min 28 s, reaches 7 of 7 symptomatic services, onset order matches hop order, type compatible with latency and timeout.",
      query: `MATCH (c:Change {id: $cid})-[:APPLIED_TO]->(o:Service)
UNWIND $symptomatic AS sid
MATCH (s:Service {id: sid})
CALL algo.SPpaths({sourceNode: o, targetNode: s, relTypes: ['IMPACTS'], maxLen: 6, relDirection: 'outgoing', pathCount: 1}) YIELD path
RETURN sid, length(path) AS hops, [n IN nodes(path) | n.id] AS via
ORDER BY hops, sid`,
      params: { cid: "chg-0418", symptomatic: HERO_SYMPTOMATIC.length },
    },
    {
      id: "hyp-2", suspect: "chg-0411",
      statement: "chg-0411 (flag flip checkout.bundle_pricing) explains the symptom set.",
      tool: "explain_symptoms", expected: "qualifies = true",
      state: "REFUTED",
      summary: "Refuted at clause order: svc-order-api degraded 71 s before svc-checkout although it is 1 hop further from the flag.",
      query: `MATCH (c:Change {id: $cid})-[:APPLIED_TO]->(o:Service)
MATCH (sym:Symptom)-[:OBSERVED_ON]->(s:Service)
CALL algo.SPpaths({sourceNode: o, targetNode: s, relTypes: ['IMPACTS'], maxLen: 6, relDirection: 'outgoing', pathCount: 1}) YIELD path
RETURN s.id, length(path) AS hops, sym.onset AS onset ORDER BY hops`,
      params: { cid: "chg-0411" },
    },
    {
      id: "hyp-3", suspect: "chg-0397",
      statement: "chg-0397 (tax rule table refresh) explains the symptom set.",
      tool: "explain_symptoms", expected: "qualifies = true",
      state: "REFUTED",
      summary: "Refuted at clause coverage: reaches 4 of 7 symptomatic services; svc-payments-gw and svc-mobile-api are unreachable.",
      query: `MATCH (c:Change {id: $cid})-[:APPLIED_TO]->(o:Service)
CALL algo.BFS(o, 6, 'IMPACTS') YIELD nodes
RETURN [n IN nodes | n.id] AS reach`,
      params: { cid: "chg-0397" },
    },
    {
      id: "hyp-4", suspect: "chg-0418",
      statement: "With chg-0418 removed, no remaining change explains the alert (necessity).",
      tool: "check_necessity", expected: "qualifying_rivals = 0",
      state: "HOLDS",
      summary: "Necessary: on the snapshot with chg-0418 ended, 0 of 311 rivals qualify.",
      query: `CALL ic.without_change($snap, $cid) YIELD graph
UNWIND $rivals AS rid
MATCH (r:Change {id: rid}) CALL ic.qualifies(graph, r) YIELD ok
RETURN count(CASE WHEN ok THEN 1 END) AS qualifying_rivals`,
      params: { snap: G("true"), cid: "chg-0418", rivals: "[311 change ids]" },
    },
  ];

  let prev: string | null = null;
  let t = at + 200;
  const hashes: string[] = [];
  proofs.forEach((p, i) => {
    b.add("hypothesis", "triage", t, {
      id: p.id, suspect: p.suspect, statement: p.statement,
      falsifier_tool: p.tool, expected: p.expected, state: "REGISTERED",
    });
    tool(b, t + 350, "triage", "test_hypothesis", G("true"), [p.query], [p.params],
      [p.suspect, ...HERO_PATH.slice(0, 3)], [38, 22, 12, 96][i], { hypothesis: p.id, state: p.state });
    const hash = fakeHash(`${prev ?? "genesis"}|${p.id}|${p.state}|${p.summary}`);
    const proof: ProofData = {
      hypothesis_id: p.id, position: i + 1, hash, prev_hash: prev, state: p.state,
      summary: p.summary, query: p.query, params: p.params,
    };
    b.add("proof", "triage", t + 700, proof);
    hashes.push(hash);
    prev = hash;
    t += 1500;
  });

  // absence certificate
  tool(b, t, "triage", "absence_certificate", G("true"),
    [`MATCH (c:Change) WHERE c.valid_from >= $win_from AND c.valid_from <= $win_to AND c.id <> $root
WITH c, ic.first_failed_clause(c, $symptomatic) AS failed
RETURN c.id, c.service, failed ORDER BY failed, c.id`],
    [{ win_from: win.from, win_to: win.to, root: HERO_ROOT_CHANGE, symptomatic: HERO_SYMPTOMATIC.length }],
    ["chg-0411", "chg-0397", "chg-0421"], 52, { checked: 311, qualifying: 0 });
  const cert: CertificateData = {
    services_checked: 37,
    window: win,
    qualifying: 0,
    suspect: HERO_ROOT_CHANGE,
    checked: rivals,
    hash: fakeHash(`cert|${hashes[hashes.length - 1]}|${rivals.length}`),
  };
  b.add("certificate", "triage", t + 500, cert);
  t += 1500;

  // precedent
  tool(b, t, "triage", "find_precedent", "ledger",
    [
      `CALL db.idx.vector.queryNodes('Incident', 'embedding', 5, vecf32($vec)) YIELD node, score
RETURN node.id, node.title, score`,
      `CALL db.idx.fulltext.queryNodes('Incident', $q) YIELD node, score
RETURN node.id, node.title, score LIMIT 5`,
      `MATCH (i:Incident)-[:CAUSED_BY]->(c:Change)-[:APPLIED_TO]->(o:Service)
WHERE ic.path_shape(i) = $shape
RETURN i.id, c.id, o.id LIMIT 5`,
    ],
    [{ vec: "[384 floats]" }, { q: "pricing deploy checkout latency" }, { shape: "deploy>pricing>quote>cart>checkout" }],
    ["inc-0233", "inc-0197", "inc-0141"], 71, { matches: 3 });
  b.add("narration", "triage", t + 600, {
    topic: "precedent",
    text: "Three past incidents match. Two by meaning, one by wording, and the most recent has the same causal-path shape: a pricing deploy reaching checkout through the quote engine.",
    matches: [
      { incident: "inc-0233", title: "Checkout 5xx after pricing@v37 rollout", method: "path_shape", score: 1.0, cause: "chg-0233 deploy pricing@v37, rolled back" },
      { incident: "inc-0197", title: "Quote engine timeouts, stale pricing cache", method: "semantic", score: 0.87, cause: "cache TTL change on svc-pricing" },
      { incident: "inc-0141", title: "Checkout latency during pricing deploy", method: "fuzzy", score: 0.74, cause: "pricing@v29 cold start storm" },
    ],
  });
  t += 1700;

  b.add("verdict", "triage", t, {
    outcome: "DIAGNOSED",
    headline:
      "chg-0418, deploy pricing@v42 on svc-pricing@us-east, is the only change that qualifies. Removing it leaves no explanation.",
    root_cause: {
      change: "chg-0418", service: pricing, kind: "deploy",
      summary: "deploy pricing@v42", author: "m.okafor", deployed_at: "2026-10-09T12:03:12Z",
      known_at_alert: true, lead_s: 268, earliest_onset: "2026-10-09T12:07:40Z",
    },
    paths: [{ label: "Causal path", change: "chg-0418", service: pricing, nodes: HERO_PATH }],
    chain_head: hashes[hashes.length - 1],
  });
  b.add("state", "system", t + 100, { state: "DIAGNOSED" });
  t += 1200;
  b.add("handoff", "system", t, {
    to: "remediation",
    summary: "Roll back pricing@v42 to v41 on svc-pricing@us-east. Rehearse on a cloned world first.",
  });
  b.add("state", "system", t + 100, { state: "REHEARSING" });
  t += 1800;
  b.add("rehearsal", "remediation", t, {
    result: "CLEARED", ms: 1240,
    detail: "Cloned the true snapshot, ended chg-0418, restored pricing@v41. No change explains any of the 7 symptoms. Rollback clears the fault.",
  });
  b.add("state", "system", t + 100, { state: "ACTING" });
  t += 1400;
  const actions: ActionData[] = [
    { kind: "pr", status: "ok", label: "Rollback PR #482 opened", url: "https://github.com/acme-shop/pricing/pull/482", detail: "Reverts pricing@v42; body carries the proof chain head." },
    { kind: "commit_status", status: "ok", label: "ic/proof status: success", detail: `Chain head ${hashes[hashes.length - 1].slice(0, 12)} posted to the PR head commit.` },
    { kind: "page", status: "ok", label: "Discord page sent to Priya Raman", detail: "#oncall-checkout, with PR link and both certificate hashes." },
  ];
  actions.forEach((a, i) => b.add("action", "remediation", t + i * 900, a));
  t += actions.length * 900;
  b.add("state", "system", t, { state: "PR_OPEN" });
  b.add("state", "system", t + 300, { state: "RESOLVED" });
  b.add("memory", "system", t + 900, {
    written: ["Incident inc-20261009-0071", "CAUSED_BY chg-0418", "Path shape deploy>pricing>quote>cart>checkout"],
    detail: "Wrote the incident, its proofs, and the path shape back to the ledger. The next pricing deploy incident resolves from precedent first.",
  });
  b.add("state", "system", t + 1100, { state: "LEARNED" });
  b.add("done", "system", t + 1300, { ms: t + 1300 });
  return b.events;
}

export const HERO_EVENTS: RunEvent[] = buildHeroEvents();
