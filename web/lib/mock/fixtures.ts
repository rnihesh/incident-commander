import type { RunEvent } from "../events";
import type { Clause, EliminatedChange, ProofData } from "../payloads";
import { EventBuilder, T0, fakeHash } from "./builders";
import { HERO_EVENTS, HERO_ROOT_CHANGE, buildRivals } from "./hero-run";
import { HERO_ALERTING, HERO_LATE, HERO_PATH, HERO_SYMPTOMATIC, serviceId } from "./hero-graph";

const win = { from: "2026-10-09T11:36:41Z", to: "2026-10-09T14:06:41Z" };

function head(b: EventBuilder, extra: { recorded?: boolean; title?: string } = {}) {
  b.add("run_started", "system", 0, {
    incident_id: "inc-20261009-0072",
    title: extra.title ?? "svc-checkout@us-east: p99 latency and 5xx spike",
    recorded: extra.recorded,
    alert: {
      service: HERO_ALERTING,
      signal: "p99 latency 4.8 s, 5xx 11.2%",
      severity: "SEV-2",
      fired_at: "2026-10-09T14:06:41Z",
    },
  });
  b.add("reflex", "system", 90, {
    ms: 91, label: "pre-snapshot", blast_radius: 37,
    suspects: [
      { change: "chg-0418", service: serviceId("pricing"), summary: "deploy pricing@v42" },
      { change: "chg-0423", service: serviceId("tax"), summary: "deploy tax@v19" },
      { change: "chg-0391", service: serviceId("config-store"), summary: "config ttl_seconds 300 to 30" },
    ],
    oncall: { name: "Priya Raman", team: "Checkout Platform", channel: "#oncall-checkout" },
  });
  b.add("state", "system", 150, { state: "TRIAGING" });
  b.add("snapshot", "system", 1500, {
    build_ms: 141,
    graphs: [{ name: "true", nodes: 4318, edges: 11902 }, { name: "known", nodes: 4316, edges: 11897 }],
    symptomatic: HERO_SYMPTOMATIC,
    late_arriving: HERO_LATE,
  });
}

function funnel(b: EventBuilder, at: number, step: number, clause: Clause, before: number, after: number, elim: EliminatedChange[], top: string[]) {
  b.add("funnel", "triage", at, {
    step, clause, label: clause, before, after,
    cypher: `// ${clause} cut\nMATCH (c:Change) WHERE c.id IN $cands RETURN c.id`,
    params: { cands: `[${before} change ids]` },
    eliminated: elim, top_suspects: top,
  });
}

function proofChain(items: { id: string; suspect: string; statement: string; state: "HOLDS" | "REFUTED"; summary: string }[], b: EventBuilder, start: number) {
  let prev: string | null = null;
  let t = start;
  items.forEach((p, i) => {
    b.add("hypothesis", "triage", t, {
      id: p.id, suspect: p.suspect, statement: p.statement,
      falsifier_tool: "explain_symptoms", expected: "qualifies = true", state: "REGISTERED",
    });
    const hash = fakeHash(`${prev ?? "genesis"}|${p.id}|${p.state}`);
    const proof: ProofData = {
      hypothesis_id: p.id, position: i + 1, hash, prev_hash: prev, state: p.state, summary: p.summary,
      query: "MATCH (c:Change {id: $cid})-[:APPLIED_TO]->(o:Service)\nCALL algo.BFS(o, 6, 'IMPACTS') YIELD nodes\nRETURN [n IN nodes | n.id] AS reach",
      params: { cid: p.suspect },
    };
    b.add("proof", "triage", t + 500, proof);
    prev = hash;
    t += 1200;
  });
  return { end: t, head: prev as string | null };
}

const rivals = buildRivals();
const pathB = ["tax", "tax-calc", "cart", "checkout"].map(serviceId);

// INCONCLUSIVE: two suspects each explain the full symptom set (AE1).
export const INCONCLUSIVE_EVENTS: RunEvent[] = (() => {
  const b = new EventBuilder("run_inc01", T0);
  head(b);
  funnel(b, 2600, 1, "after_onset", 312, 41, rivals.filter((r) => r.clause === "after_onset"), ["chg-0418", "chg-0423", "chg-0397"]);
  funnel(b, 4000, 2, "origin", 41, 9, rivals.filter((r) => r.clause === "origin"), ["chg-0418", "chg-0423"]);
  funnel(b, 5400, 3, "coverage", 9, 3, rivals.filter((r) => r.clause === "coverage"), ["chg-0418", "chg-0423"]);
  funnel(b, 6800, 4, "order", 3, 2, [{ change: "chg-0411", service: serviceId("feature-flags"), clause: "order" }], ["chg-0418", "chg-0423"]);
  funnel(b, 8200, 5, "signature", 2, 2, [], ["chg-0418", "chg-0423"]);
  const { end, head: h } = proofChain(
    [
      { id: "hyp-1", suspect: "chg-0418", statement: "chg-0418 qualifies.", state: "HOLDS", summary: "Qualifies on all four clauses." },
      { id: "hyp-2", suspect: "chg-0423", statement: "chg-0423 qualifies.", state: "HOLDS", summary: "Also qualifies on all four clauses: reaches 7 of 7 symptomatic services." },
    ],
    b, 9400,
  );
  b.add("certificate", "triage", end + 300, {
    services_checked: 37, window: win, qualifying: 1, suspect: "chg-0418",
    checked: [...rivals.filter((r) => r.clause !== "signature"), ],
    hash: fakeHash("cert-inc"),
  });
  b.add("verdict", "triage", end + 1500, {
    outcome: "INCONCLUSIVE",
    headline: "chg-0418 and chg-0423 each explain every symptom. Neither is necessary once the other is removed.",
    paths: [
      { label: "Path A", change: "chg-0418", service: serviceId("pricing"), nodes: HERO_PATH },
      { label: "Path B", change: "chg-0423", service: serviceId("tax"), nodes: pathB },
    ],
    chain_head: h,
  });
  b.add("state", "system", end + 1600, { state: "INCONCLUSIVE" });
  b.add("action", "system", end + 2400, { kind: "page", status: "ok", label: "Discord page sent to Priya Raman", detail: "Both causal paths attached. No PR opened." });
  b.add("done", "system", end + 2800, { ms: end + 2800 });
  return b.events;
})();

// REFUSED: rollback rehearsal leaves another change explaining part of the symptoms (AE4).
export const REFUSED_EVENTS: RunEvent[] = (() => {
  const b = new EventBuilder("run_ref01", T0);
  head(b);
  funnel(b, 2600, 1, "after_onset", 312, 41, rivals.filter((r) => r.clause === "after_onset"), ["chg-0418", "chg-0423"]);
  funnel(b, 4000, 2, "origin", 41, 1, rivals.filter((r) => r.clause !== "after_onset" && r.clause !== "signature"), ["chg-0418"]);
  const { end, head: h } = proofChain(
    [{ id: "hyp-1", suspect: "chg-0418", statement: "chg-0418 qualifies.", state: "HOLDS", summary: "Qualifies on all four clauses." }],
    b, 5400,
  );
  b.add("verdict", "triage", end + 600, {
    outcome: "DIAGNOSED",
    headline: "chg-0418, deploy pricing@v42, qualifies. Rehearsal will check that rolling it back is enough.",
    root_cause: { change: "chg-0418", service: serviceId("pricing"), kind: "deploy", summary: "deploy pricing@v42", known_at_alert: true },
    paths: [{ label: "Causal path", change: "chg-0418", service: serviceId("pricing"), nodes: HERO_PATH }],
    chain_head: h,
  });
  b.add("state", "system", end + 700, { state: "DIAGNOSED" });
  b.add("handoff", "system", end + 1500, { to: "remediation", summary: "Roll back pricing@v42; rehearse first." });
  b.add("state", "system", end + 1600, { state: "REHEARSING" });
  b.add("rehearsal", "remediation", end + 3200, {
    result: "REFUSED", ms: 1310,
    detail: "After the rollback, 3 of 7 symptoms remain explained by another change.",
    surviving: { change: "chg-0423", path: ["svc-tax@us-east", "svc-tax-calc@us-east", "svc-cart@us-east"] },
  });
  b.add("state", "system", end + 3300, { state: "REFUSED" });
  b.add("action", "system", end + 4100, { kind: "page", status: "ok", label: "Discord page sent to Priya Raman", detail: "Refusal reason and surviving path attached. No PR opened." });
  b.add("done", "system", end + 4400, { ms: end + 4400 });
  return b.events;
})();

// DEGRADED: agent loop hit its step cap; deterministic funnel concluded.
export const DEGRADED_EVENTS: RunEvent[] = (() => {
  const b = new EventBuilder("run_deg01", T0);
  head(b);
  b.add("narration", "triage", 2200, { text: "Step cap reached after 25 steps. Falling back to the deterministic funnel." });
  funnel(b, 3000, 1, "after_onset", 312, 41, rivals.filter((r) => r.clause === "after_onset"), ["chg-0418", "chg-0411"]);
  funnel(b, 4200, 2, "origin", 41, 9, rivals.filter((r) => r.clause === "origin"), ["chg-0418", "chg-0411"]);
  funnel(b, 5400, 3, "coverage", 9, 2, rivals.filter((r) => r.clause === "coverage"), ["chg-0418", "chg-0411"]);
  funnel(b, 6600, 4, "order", 2, 1, [{ change: "chg-0411", service: serviceId("feature-flags"), clause: "order" }], ["chg-0418"]);
  const { end, head: h } = proofChain(
    [{ id: "hyp-1", suspect: "chg-0418", statement: "chg-0418 qualifies.", state: "HOLDS", summary: "Qualifies on all four clauses." }],
    b, 7800,
  );
  b.add("verdict", "triage", end + 600, {
    outcome: "DIAGNOSED", degraded: true,
    headline: "chg-0418, deploy pricing@v42, is the only qualifying change (deterministic fallback).",
    root_cause: { change: "chg-0418", service: serviceId("pricing"), kind: "deploy", summary: "deploy pricing@v42", known_at_alert: true },
    paths: [{ label: "Causal path", change: "chg-0418", service: serviceId("pricing"), nodes: HERO_PATH }],
    chain_head: h,
  });
  b.add("state", "system", end + 700, { state: "DIAGNOSED" });
  b.add("done", "system", end + 1000, { ms: end + 1000 });
  return b.events;
})();

// RECORDED: the hero stream, flagged as a saved run.
export const RECORDED_EVENTS: RunEvent[] = HERO_EVENTS.map((e) =>
  e.type === "run_started" ? { ...e, data: { ...e.data, recorded: true } } : e,
);

export const RATE_LIMITED_EVENTS: RunEvent[] = new EventBuilder("run_rl01", T0)
  .add("error", "system", 0, {
    code: "rate_limited",
    message: "This session has used its 3 investigations for the hour.",
    retry_after_s: 1260,
  }).events;

export const EXPIRED_EVENTS: RunEvent[] = new EventBuilder("run_ex01", T0)
  .add("error", "system", 0, {
    code: "session_expired",
    message: "Sessions end after 30 minutes without activity.",
  }).events;

export { HERO_ROOT_CHANGE };
