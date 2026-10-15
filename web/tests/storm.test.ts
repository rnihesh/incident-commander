import { describe, expect, it } from "vitest";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { SCHEMA_VERSION, type RunEvent } from "../lib/events";
import { counter, initialStorm, reduceStorm, reduceStormAll, stormCanvas, type StormIncident } from "../lib/storm";
import { IntakeStrip, StormCard } from "../components/Storm";

const ev = (id: number, type: string, data: unknown): RunEvent =>
  ({ schema_version: SCHEMA_VERSION, id, run_id: "r", ts: id, type, agent: "system", data }) as unknown as RunEvent;

const T = 1_790_900_000;
const DEPLOY: StormIncident = {
  component: 1,
  symptomatic: ["svc-orders@us-east", "svc-cart@us-east"],
  alerts: 190,
  first_page: T,
  explaining: [{ id: "chg-00010", kind: "deploy", service_id: "svc-orders@us-east", summary: "Deploy v1.2.4: bump deps", event_time: T - 600 }],
  jointly_caused: false,
  joint: null,
  alternatives: [],
  unexplained: [],
  paths: [{ label: "chg-00010 -> svc-cart@us-east", nodes: ["chg-00010", "svc-orders@us-east", "svc-cart@us-east"] }],
  checked: 3,
};
const JOINT: StormIncident = {
  component: 2,
  symptomatic: ["svc-tax@eu-west", "svc-checkout@eu-west"],
  alerts: 122,
  first_page: T + 60,
  explaining: [
    { id: "chg-00007", kind: "config", service_id: "svc-tax@eu-west", summary: "Set timeout_ms to 90", event_time: T - 3600 },
    { id: "chg-00011", kind: "flag", service_id: "svc-tax@eu-west", summary: "Flip flag async_pricing off->on", event_time: T - 300 },
  ],
  jointly_caused: true,
  joint: {
    flag_change: "chg-00011",
    config_change: "chg-00007",
    flag: "flg-00001",
    config: "cfg-00001",
    service_id: "svc-tax@eu-west",
    solo: {
      "chg-00011": { from: T - 20000, to: T - 15000, seconds: 5000, lead_s: 360 },
      "chg-00007": { from: T - 3600, to: T - 300, seconds: 3300, lead_s: 360 },
    },
  },
  alternatives: [],
  unexplained: [],
  paths: [{ label: "chg-00011 -> svc-checkout@eu-west", nodes: ["chg-00011", "svc-tax@eu-west", "svc-checkout@eu-west"] }],
  checked: 4,
};

const EVENTS: RunEvent[] = [
  ev(0, "run_started", { storm_id: "storm-001", storm: true, title: "Alert storm storm-001", pages: 312 }),
  ev(1, "snapshot", { t: T + 1800 }),
  ev(2, "storm", { phase: "intake", received: 150, total: 312, services_paging: 3 }),
  ev(3, "storm", { phase: "intake", received: 312, total: 312, services_paging: 4 }),
  ev(4, "tool_call", { tool: "collapse_storm", result: {}, cypher: ["CALL algo.WCC(...)"], params: [{}], nodes_touched: [], ms: 70, graph: "g" }),
  ev(5, "storm", { phase: "grouped", pages_in: 312, symptomatic_services: 4, candidates_checked: 7, components: [{ component: 1, alerts: 190, services: DEPLOY.symptomatic }, { component: 2, alerts: 122, services: JOINT.symptomatic }] }),
  ev(6, "storm", { phase: "incident", ...DEPLOY }),
  ev(7, "storm", { phase: "incident", ...JOINT }),
  ev(8, "storm", {
    phase: "chokepoints",
    chokepoints: [{ service_id: "svc-tax@eu-west", score: 6 }, { service_id: "svc-elsewhere@ap-south", score: 2 }],
    area: {
      nodes: [...DEPLOY.symptomatic, ...JOINT.symptomatic, "svc-elsewhere@ap-south"].map((id, i) => ({ id, name: id, x: i * 10, y: i * 5, symptomatic: !id.includes("elsewhere") })),
      edges: [{ source: "svc-orders@us-east", target: "svc-cart@us-east" }, { source: "svc-tax@eu-west", target: "svc-checkout@eu-west" }],
    },
  }),
  ev(9, "storm", { phase: "collapsed", pages_in: 312, incidents: 2, jointly_caused: 1, tool_ms: 70 }),
  ev(10, "done", { ms: 900 }),
];

describe("storm reducer", () => {
  it("counts pages in batches, then collapses them into incidents", () => {
    const mid = reduceStormAll(EVENTS.slice(0, 3));
    expect(counter(mid)).toEqual({ pages: 150, incidents: null });
    expect(mid.batches).toEqual([150]);
    const s = reduceStormAll(EVENTS);
    expect(s.stormId).toBe("storm-001");
    expect(s.batches).toEqual([150, 162]);
    expect(counter(s)).toEqual({ pages: 312, incidents: 2 });
    expect(s.incidents.map((i) => i.jointly_caused)).toEqual([false, true]);
    expect(s.done && s.tool?.tool).toBe("collapse_storm");
    // Duplicate delivery is ignored.
    expect(reduceStorm(s, EVENTS[6]).incidents).toHaveLength(2);
    expect(reduceStorm(initialStorm, ev(0, "storm", { phase: "unknown" })).incidents).toEqual([]);
  });

  it("filters the canvas to one incident and marks chokepoints", () => {
    const s = reduceStormAll(EVENTS);
    const all = stormCanvas(s, null);
    expect(all.nodes).toHaveLength(5);
    expect(all.graph.chokepoints).toEqual(["svc-tax@eu-west", "svc-elsewhere@ap-south"]);
    const two = stormCanvas(s, 2);
    expect(two.nodes.map((n) => n.id).sort()).toEqual(["svc-checkout@eu-west", "svc-tax@eu-west"]);
    expect(two.edges).toEqual([{ source: "svc-tax@eu-west", target: "svc-checkout@eu-west" }]);
    expect(two.graph.symptomatic).toEqual(JOINT.symptomatic);
    expect(two.graph.suspects).toEqual(["svc-tax@eu-west"]);
    expect(two.graph.chokepoints).toEqual(["svc-tax@eu-west"]);
  });
});

describe("storm cards", () => {
  it("renders a single-cause card with its explaining change", () => {
    const html = renderToStaticMarkup(createElement("ol", null, createElement(StormCard, { incident: DEPLOY })));
    expect(html).toContain("incident 1");
    expect(html).toContain("chg-00010");
    expect(html).toContain("190 pages");
    expect(html).not.toContain("jointly caused");
  });

  it("gives a joint cause its own treatment: A AND B, and how long each ran alone", () => {
    const html = renderToStaticMarkup(createElement("ol", null, createElement(StormCard, { incident: JOINT, selected: true })));
    expect(html).toContain("jointly caused");
    expect(html).toMatch(/chg-00011<\/span> <span[^>]*>AND<\/span> <span[^>]*>chg-00007/);
    expect(html).toContain("ran alone 83 min with no symptom");
    expect(html).toContain('aria-pressed="true"');
    expect(html).toContain("border-tampered");
  });

  it("flags an incident nothing explains", () => {
    const html = renderToStaticMarkup(
      createElement("ol", null, createElement(StormCard, { incident: { ...DEPLOY, explaining: [], unexplained: DEPLOY.symptomatic } })),
    );
    expect(html).toContain("No change in the window explains these pages.");
  });

  it("shows the page counter collapsing", () => {
    const html = renderToStaticMarkup(createElement(IntakeStrip, { s: reduceStormAll(EVENTS) }));
    expect(html).toContain(">312<");
    expect(html).toContain(">2<");
    expect(html).toContain("incidents");
  });
});
