import { describe, expect, it } from "vitest";
import { SCHEMA_VERSION, type RunEvent } from "../lib/events";
import { deriveBanners, initialState, reduce, reduceAll } from "../lib/reducer";
import { HERO_EVENTS } from "../lib/mock/hero-run";
import {
  DEGRADED_EVENTS,
  EXPIRED_EVENTS,
  INCONCLUSIVE_EVENTS,
  RATE_LIMITED_EVENTS,
  RECORDED_EVENTS,
  REFUSED_EVENTS,
} from "../lib/mock/fixtures";

const kinds = (events: RunEvent[], upTo?: number) =>
  deriveBanners(reduceAll(upTo === undefined ? events : events.slice(0, upTo))).map((b) => b.kind);

describe("hero stream", () => {
  const s = reduceAll(HERO_EVENTS);

  it("reaches DIAGNOSED with chg-0418 as root cause", () => {
    expect(s.verdict?.outcome).toBe("DIAGNOSED");
    expect(s.verdict?.root_cause?.change).toBe("chg-0418");
    expect(s.phase).toBe("LEARNED");
    expect(s.done).toBe(true);
  });

  it("produces the 312 to 1 funnel", () => {
    expect(s.funnel.map((f) => f.clause)).toEqual(["after_onset", "origin", "coverage", "order", "signature"]);
    expect([s.funnel[0].before, ...s.funnel.map((f) => f.after)]).toEqual([312, 41, 9, 2, 1, 1]);
    expect(s.funnel.reduce((a, f) => a + f.eliminated.length, 0)).toBe(311);
  });

  it("builds a linked proof chain", () => {
    expect(s.proofs).toHaveLength(4);
    expect(s.proofs[0].prev_hash).toBeNull();
    s.proofs.slice(1).forEach((p, i) => expect(p.prev_hash).toBe(s.proofs[i].hash));
    expect(s.proofs.map((p) => p.state)).toEqual(["HOLDS", "REFUTED", "REFUTED", "HOLDS"]);
    expect(s.proofs[0].hypothesis?.suspect).toBe("chg-0418");
    expect(s.verdict?.chain_head).toBe(s.proofs[3].hash);
  });

  it("records the certificate, tool calls, actions and memory", () => {
    expect(s.certificate?.qualifying).toBe(0);
    expect(s.certificate?.checked).toHaveLength(311);
    expect(s.toolCalls.length).toBeGreaterThanOrEqual(10);
    expect(s.toolCalls.every((t) => t.cypher.length > 0 && typeof t.ms === "number")).toBe(true);
    expect(s.actions.map((a) => a.kind)).toEqual(["pr", "commit_status", "page"]);
    expect(s.rehearsal?.result).toBe("CLEARED");
    expect(s.memory).not.toBeNull();
    expect(s.precedent[0].matches).toHaveLength(3);
  });

  it("derives graph roles from events", () => {
    expect(s.graph.alerting).toBe("svc-checkout@us-east");
    expect(s.graph.paths[0].nodes[0]).toBe("svc-pricing@us-east");
    expect(Object.keys(s.graph.eliminated).length).toBeGreaterThan(20);
  });

  it("ends in a diagnosed banner without degraded", () => {
    expect(kinds(HERO_EVENTS)).toEqual(["diagnosed"]);
  });
});

describe("robustness", () => {
  it("ignores unknown event types", () => {
    const odd = { id: 0, run_id: "x", ts: 1, type: "telemetry_v2", agent: "system", data: {}, schema_version: SCHEMA_VERSION } as unknown as RunEvent;
    const spliced = [...HERO_EVENTS.slice(0, 5), odd, ...HERO_EVENTS.slice(5)].map((e, i) => ({ ...e, id: i }));
    const clean = HERO_EVENTS.map((e, i) => ({ ...e, id: i + (i >= 5 ? 1 : 0) }));
    const a = reduceAll(spliced);
    const b = reduceAll(clean);
    expect(a.unknownEvents).toBe(1);
    expect({ ...a, unknownEvents: 0 }).toEqual(b);
    expect(a.verdict?.outcome).toBe("DIAGNOSED");
  });

  it("ignores duplicate deliveries", () => {
    const once = reduceAll(HERO_EVENTS.slice(0, 8));
    expect(reduce(once, HERO_EVENTS[7])).toBe(once);
  });
});

describe("reflex", () => {
  const firstFunnel = HERO_EVENTS.findIndex((e) => e.type === "funnel");

  it("is live before the funnel and superseded after it", () => {
    expect(reduceAll(HERO_EVENTS.slice(0, firstFunnel)).reflex?.superseded).toBe(false);
    const after = reduceAll(HERO_EVENTS.slice(0, firstFunnel + 1));
    expect(after.reflex?.superseded).toBe(true);
  });

  it("flags a diff when suspects differ", () => {
    const r = reduceAll(HERO_EVENTS.slice(0, firstFunnel + 1)).reflex;
    expect(r?.diff).toEqual({ added: ["chg-0397"], removed: ["chg-0391"] });
  });

  it("has no diff when suspects match", () => {
    const events = HERO_EVENTS.slice(0, firstFunnel + 1).map((e) =>
      e.type === "funnel" ? { ...e, data: { ...e.data, top_suspects: ["chg-0418", "chg-0411", "chg-0391"] } } : e,
    );
    const r = reduceAll(events).reflex;
    expect(r?.superseded).toBe(true);
    expect(r?.diff).toBeNull();
  });
});

describe("state banners", () => {
  it("empty console", () => expect(deriveBanners(initialState).map((b) => b.kind)).toEqual(["empty"]));
  it("investigating mid-run", () => expect(kinds(HERO_EVENTS, 12)).toEqual(["investigating"]));
  it("inconclusive with rival paths", () => {
    expect(kinds(INCONCLUSIVE_EVENTS)).toEqual(["inconclusive"]);
    expect(reduceAll(INCONCLUSIVE_EVENTS).graph.paths).toHaveLength(2);
    expect(reduceAll(INCONCLUSIVE_EVENTS).actions.map((a) => a.kind)).toEqual(["page"]);
  });
  it("refused names the surviving change", () => {
    expect(kinds(REFUSED_EVENTS)).toEqual(["refused"]);
    expect(deriveBanners(reduceAll(REFUSED_EVENTS))[0].body).toContain("chg-0423");
  });
  it("degraded is shown next to the diagnosis", () => expect(kinds(DEGRADED_EVENTS)).toEqual(["diagnosed", "degraded"]));
  it("recorded run", () => expect(kinds(RECORDED_EVENTS)).toEqual(["recorded", "diagnosed"]));
  it("rate limited", () => expect(kinds(RATE_LIMITED_EVENTS)).toEqual(["rate_limited"]));
  it("session expired", () => expect(kinds(EXPIRED_EVENTS)).toEqual(["session_expired"]));
  it("failed and action retry", () => {
    const failed = reduceAll([
      { id: 0, run_id: "r", ts: 0, type: "run_started", agent: "system", schema_version: 1, data: { incident_id: "i", title: "t", alert: { service: "a", signal: "", severity: "", fired_at: "" } } },
      { id: 1, run_id: "r", ts: 1, type: "error", agent: "system", schema_version: 1, data: { code: "failed", message: "boom" } },
      { id: 2, run_id: "r", ts: 2, type: "error", agent: "remediation", schema_version: 1, data: { code: "action_failed", message: "GitHub 502", action: "open PR" } },
    ]);
    expect(deriveBanners(failed).map((b) => b.kind)).toEqual(["failed", "action_error"]);
    expect(failed.phase).toBe("FAILED");
  });
});
