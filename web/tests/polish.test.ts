import { describe, expect, it } from "vitest";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { SCHEMA_VERSION, type RunEvent } from "../lib/events";
import { deriveBanners, degradedCopy, initialState, reduce, reduceAll } from "../lib/reducer";
import { fitNodes, liveGraphView, toLiveGraph, CANVAS_H, CANVAS_PAD, CANVAS_W } from "../lib/liveGraph";
import type { IncidentGraphResponse } from "../lib/api";
import { HERO_EVENTS } from "../lib/mock/hero-run";
import { DEGRADED_EVENTS } from "../lib/mock/fixtures";
import { Reflex } from "../components/Reflex";
import { Precedent } from "../components/Precedent";

const node = (id: string, x: number, y: number, o: Partial<IncidentGraphResponse["nodes"][0]> = {}) => ({
  id, name: id, region: "r", tier: 0, x, y, symptomatic: false, onset: null, signature: null, alerting: false, ...o,
});
const RESP: IncidentGraphResponse = {
  incident_id: "inc-1", t: 1_790_000_000, alert_service_id: "c",
  nodes: [node("a", -10, 100), node("b", 90, 100, { symptomatic: true }), node("c", 90, 300, { alerting: true, symptomatic: true }), node("d", -10, 300)],
  edges: [{ source: "a", target: "b" }, { source: "b", target: "c" }, { source: "x", target: "c" }],
  changes: [{ id: "chg-1", service_id: "a", kind: "deploy", valid_from: 0, recorded_at: null }],
};
const ev = (id: number, type: string, data: unknown): RunEvent =>
  ({ v: SCHEMA_VERSION, id, run_id: "r", ts: id, type, agent: "system", data }) as unknown as RunEvent;

describe("live graph mapping", () => {
  it("rescales into the padded box, preserving aspect", () => {
    const n = fitNodes(RESP.nodes);
    const xs = n.map((p) => p.x);
    const ys = n.map((p) => p.y);
    expect(Math.min(...xs)).toBeGreaterThanOrEqual(CANVAS_PAD - 1e-9);
    expect(Math.max(...xs)).toBeLessThanOrEqual(CANVAS_W - CANVAS_PAD + 1e-9);
    expect(Math.min(...ys)).toBeCloseTo(CANVAS_PAD);
    expect(Math.max(...ys)).toBeCloseTo(CANVAS_H - CANVAS_PAD);
    // 100 wide by 200 tall keeps its 1:2 ratio
    expect((Math.max(...ys) - Math.min(...ys)) / (Math.max(...xs) - Math.min(...xs))).toBeCloseTo(2);
  });
  it("handles a single node and drops dangling edges", () => {
    expect(fitNodes([node("a", 5, 5)])[0]).toMatchObject({ x: CANVAS_W / 2, y: CANVAS_H / 2 });
    expect(toLiveGraph(RESP).edges).toHaveLength(2);
  });
  it("derives roles from flags and reducer state", () => {
    let s = reduce(initialState, ev(0, "run_started", { incident_id: "inc-1", title: "t", alert: { service: "c", signal: "", severity: "", fired_at: "" } }));
    s = reduce(s, ev(1, "funnel", {
      step: 1, clause: "origin", label: "", before: 2, after: 1, cypher: "",
      eliminated: [{ change: "chg-2", service: "d", clause: "origin", late: true }], top_suspects: ["chg-1"],
    }));
    let v = liveGraphView(RESP, s);
    expect(v.alerting).toBe("c");
    expect(v.symptomatic.sort()).toEqual(["b", "c"]);
    expect(v.suspects).toEqual(["a"]);
    expect(v.eliminated.d.clause).toBe("origin");
    expect(v.late).toEqual(["d"]);
    s = reduce(s, ev(2, "verdict", { outcome: "DIAGNOSED", headline: "h", paths: [{ label: "p", change: "chg-1", service: "a", nodes: ["a", "b", "c"] }] }));
    v = liveGraphView(RESP, s);
    expect(v.paths[0].nodes).toEqual(["a", "b", "c"]);
    expect(v.suspects).toEqual(["a"]);
  });
});

describe("degraded copy", () => {
  it.each([
    ["no_llm", /No LLM configured/],
    ["step_cap", /step cap/],
    ["spend_cap", /spend cap/],
    ["llm_error", /LLM call failed/],
    ["timeout", /timed out/],
    [undefined, /did not complete/],
    ["weird", /did not complete/],
  ])("%s", (reason, re) => {
    expect(degradedCopy(reason)).toMatch(re);
  });
  it("uses the verdict's reason in the banner, and never claims caps for no_llm", () => {
    const events = HERO_EVENTS.map((e) => (e.type === "verdict" ? { ...e, data: { ...e.data, degraded: true, degraded_reason: "no_llm" } } : e));
    const b = deriveBanners(reduceAll(events)).find((x) => x.kind === "degraded");
    expect(b?.body).toMatch(/^No LLM configured/);
    expect(b?.body).not.toMatch(/cap/);
  });
  it("falls back to the narration reason and neutral copy", () => {
    const withNarr = reduceAll([HERO_EVENTS[0], ev(1, "narration", { text: "x", degraded_reason: "spend_cap" }), ...HERO_EVENTS.filter((e) => e.type === "verdict").map((e) => ({ ...e, id: 9, data: { ...e.data, degraded: true } }))]);
    expect(deriveBanners(withNarr).find((x) => x.kind === "degraded")?.body).toMatch(/spend cap/);
    expect(deriveBanners(reduceAll(DEGRADED_EVENTS)).find((x) => x.kind === "degraded")?.body).toMatch(/did not complete/);
  });
});

describe("reflex strip", () => {
  const base = { ms: 4, label: "pre-snapshot" as const, suspects: [], oncall: { name: "N" }, superseded: false, diff: null };
  const html = (r: object) => renderToStaticMarkup(createElement(Reflex, { reflex: { ...base, ...r } }));
  it("shows symptomatic and search space", () => {
    const h = html({ blast_radius: 6, search_space: 42 });
    expect(h).toMatch(/Symptomatic/);
    expect(h).toMatch(/Search space/);
    expect(h).not.toMatch(/Blast radius/);
  });
  it("falls back when a field is missing", () => {
    expect(html({ blast_radius: 6 })).not.toMatch(/Search space/);
    expect(html({ search_space: 9 })).not.toMatch(/Symptomatic/);
    expect(html({})).not.toMatch(/reflex-scope/);
  });
});

describe("precedent payload", () => {
  it("accepts the precedents shape additively", () => {
    const s = reduce(initialState, ev(0, "narration", {
      topic: "precedent",
      precedents: [{ incident_id: "inc-9", postmortem_id: "pm-9", title: "Bad deploy", methods: { vector: 0.8, fulltext: 0.4, fingerprint: 1 }, remediation: "Rollback", resolved_minutes: 12 }],
    }));
    const m = s.precedent[0].matches[0];
    expect(m).toMatchObject({ incident: "inc-9", method: "path_shape", cause: "Rollback", resolved_minutes: 12 });
    const h = renderToStaticMarkup(createElement(Precedent, { entries: s.precedent }));
    expect(h).toMatch(/resolved in 12 min/);
  });
});

describe("chain head", () => {
  it("verdict carries chain_head", () => {
    expect(reduceAll(HERO_EVENTS.filter((e) => e.type !== "done")).verdict?.chain_head).toBeTruthy();
  });
});

import { shortSvcs } from "../components/ui";
describe("shortSvcs", () => {
  it("keeps the region only for names shared across regions", () => {
    expect(shortSvcs(["svc-a@us", "svc-b@us", "svc-b@eu"])).toEqual(["a", "b@us", "b@eu"]);
  });
});

import { CausalPathList } from "../components/CausalPathList";
import { Verdict, formatLead, leadSentence } from "../components/Response";
describe("late-arriving line", () => {
  const base = reduce(initialState, ev(0, "run_started", { incident_id: "inc-1", title: "t", alert: { service: "c", signal: "", severity: "", fired_at: "" } }));
  const render = (s: typeof base) =>
    renderToStaticMarkup(createElement(CausalPathList, { graph: liveGraphView(RESP, s), onFocus: () => {} }));
  it("is hidden when the snapshot lists nothing relevant", () => {
    const s = reduce(base, ev(1, "snapshot", { build_ms: 1, graphs: [], symptomatic: ["b"], late_arriving: [], late_edges: [] }));
    expect(render(s)).not.toMatch(/Late-arriving/);
    expect(render(base)).not.toMatch(/Late-arriving/);
  });
  it("lists late services and late edges from the snapshot", () => {
    const s = reduce(base, ev(1, "snapshot", {
      build_ms: 1, graphs: [], symptomatic: ["b"],
      late_arriving: ["svc-pricing@us"], late_edges: [{ caller: "svc-cart@us", callee: "svc-quote@us" }],
    }));
    expect(s.graph.late).toEqual(["svc-pricing@us"]);
    expect(liveGraphView(RESP, s).lateEdges).toEqual([{ caller: "svc-cart@us", callee: "svc-quote@us" }]);
    const h = render(s);
    expect(h).toMatch(/Late-arriving/);
    expect(h).toMatch(/pricing/);
    expect(h).toMatch(/cart → quote/);
  });
});

describe("verdict lead time", () => {
  it("formats N min S s", () => {
    expect(formatLead(268)).toBe("4 min 28 s");
    expect(formatLead(38)).toBe("38 s");
    expect(formatLead(3600 + 125)).toBe("1 h 2 min 5 s");
  });
  it("falls back to a generic sentence without lead_s", () => {
    expect(leadSentence(undefined)).toBe("As true at alert time: the change landed before the earliest symptom.");
    expect(leadSentence(0)).not.toMatch(/\d/);
  });
  it("renders the hero's real lead time in the as-true view", () => {
    const s = reduceAll(HERO_EVENTS.filter((e) => e.type !== "done"));
    expect(s.verdict?.root_cause?.lead_s).toBe(268);
    const h = renderToStaticMarkup(createElement(Verdict, { s, view: "true" }));
    expect(h).toMatch(/landed 4 min 28 s before the earliest symptom/);
    const noLead = { ...s, verdict: { ...s.verdict!, root_cause: { ...s.verdict!.root_cause!, lead_s: undefined } } };
    const h2 = renderToStaticMarkup(createElement(Verdict, { s: noLead, view: "true" }));
    expect(h2).toMatch(/landed before the earliest symptom/);
  });
});
