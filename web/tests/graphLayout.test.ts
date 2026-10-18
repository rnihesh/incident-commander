import { describe, expect, it } from "vitest";
import { clusterLayout, layeredLayout, LAYER, pack } from "../lib/graphLayout";
import { labelNames, LABEL_MAX } from "../lib/labels";
import { toLiveGraph } from "../lib/liveGraph";

// IMPACTS edges run cause -> consumer: origin -> hop -> alert, plus side services.
const EDGES = [
  { source: "origin", target: "hop" },
  { source: "hop", target: "alert" },
  { source: "side", target: "alert" },
  { source: "far", target: "side" },
  { source: "origin", target: "other-sym" },
];
const IDS = ["alert", "hop", "origin", "side", "far", "other-sym"];
const weight = (id: string) => (id === "alert" || id === "other-sym" ? 2 : id === "origin" ? 1 : 0);

describe("layeredLayout", () => {
  const pos = layeredLayout(IDS, EDGES, "alert", weight);

  it("puts the alert in the right-hand column and each upstream hop one column left", () => {
    expect(pos.get("alert")).toMatchObject({ x: 0, y: 0 });
    expect(pos.get("hop")!.x).toBe(-LAYER.colW);
    expect(pos.get("origin")!.x).toBe(-2 * LAYER.colW);
    expect(pos.get("far")!.x).toBe(-2 * LAYER.colW);
    // A symptomatic service the alert does not reach shares the alert's column.
    expect(pos.get("other-sym")!.x).toBe(0);
  });

  it("keeps nodes in one column at least the minimum gap apart", () => {
    const cols = new Map<number, number[]>();
    for (const p of pos.values()) cols.set(p.x, [...(cols.get(p.x) ?? []), p.y]);
    for (const ys of cols.values()) {
      ys.sort((a, b) => a - b);
      for (let i = 1; i < ys.length; i++) expect(ys[i] - ys[i - 1]).toBeGreaterThanOrEqual(LAYER.gap - 1e-9);
    }
  });

  it("is deterministic", () => {
    expect([...layeredLayout(IDS, EDGES, "alert", weight)]).toEqual([...pos]);
  });
});

describe("pack", () => {
  it("leaves free nodes at their wanted height and spreads a crowd evenly around its mean", () => {
    const out = new Map<string, number>();
    pack(["a", "b", "c"], [0, 0, 100], () => 10, out);
    expect(out.get("a")).toBe(-5);
    expect(out.get("b")).toBe(5);
    expect(out.get("c")).toBe(100);
  });
});

describe("clusterLayout", () => {
  it("groups services by region column and domain disc, with captions", () => {
    const svc = (id: string, region: string, domain: string) => ({ id, name: id, region, domain, tier: 1 });
    const { pos, captions } = clusterLayout([svc("a", "us-east", "edge"), svc("b", "eu-west", "edge"), svc("c", "us-east", "data"), svc("d", "us-east", "edge")]);
    expect(pos.get("a")!.x).toBeLessThan(pos.get("b")!.x); // us-east column left of eu-west
    expect(Math.hypot(pos.get("a")!.x - pos.get("d")!.x, pos.get("a")!.y - pos.get("d")!.y)).toBeLessThan(30); // same disc
    expect(captions.filter((c) => c.kind === "region").map((c) => c.text)).toEqual(["us-east", "eu-west"]);
    expect(captions.filter((c) => c.kind === "domain")).toHaveLength(3);
  });
});

describe("labelNames", () => {
  it("adds the region only to names shared across regions and cuts long names", () => {
    const long = "x".repeat(LABEL_MAX + 5);
    const m = labelNames([
      { id: "svc-api@us-east", name: "api" },
      { id: "svc-api@eu-west", name: "api" },
      { id: "svc-db@us-east", name: "db" },
      { id: `svc-${long}@us-east`, name: long },
    ]);
    expect(m.get("svc-api@eu-west")).toBe("api@eu-west");
    expect(m.get("svc-db@us-east")).toBe("db");
    expect(m.get(`svc-${long}@us-east`)).toHaveLength(LABEL_MAX);
    expect(m.get(`svc-${long}@us-east`)!.endsWith("…")).toBe(true);
  });
});

describe("toLiveGraph layout choice", () => {
  const node = (id: string, o: { symptomatic?: boolean; alerting?: boolean } = {}) => ({
    id, name: id, x: Math.random(), y: Math.random(), symptomatic: false, onset: null, signature: null, alerting: false, ...o,
  });
  it("lays an incident out in layers towards the alert", () => {
    const g = toLiveGraph({ nodes: [node("alert", { alerting: true, symptomatic: true }), node("hop")], edges: [{ source: "hop", target: "alert" }] });
    const at = (id: string) => g.nodes.find((n) => n.id === id)!;
    expect(at("alert").x).toBeGreaterThan(at("hop").x);
    expect(at("alert").y).toBe(at("hop").y);
  });
});
