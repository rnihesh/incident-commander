import { describe, expect, it } from "vitest";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { SCHEMA_VERSION, type RunEvent } from "../lib/events";
import { initialState, reduce, reduceAll } from "../lib/reducer";
import type { LearningCurveIncident, LearningCurveResponse } from "../lib/api";
import type { RecommendationRef } from "../lib/payloads";
import { RecommendationBanner, recommendationLine } from "../components/RecommendationBanner";
import { LearningCurveChart } from "../components/LearningCurve";

const ev = (id: number, type: string, data: unknown): RunEvent =>
  ({ v: SCHEMA_VERSION, id, run_id: "r", ts: id, type, agent: "triage", data }) as unknown as RunEvent;

const REC: RecommendationRef = {
  id: "rec-082",
  display_id: "REC-082",
  text: "Declare the dependency of svc-a on svc-b",
  days_open: 3,
  incident_id: "inc-041",
  postmortem_id: "pm-041",
  services: ["svc-a@us-east", "svc-b@us-east"],
  banner: "Predicted by REC-082 (from INC-041 postmortem), open 3 days: Declare the dependency of svc-a on svc-b",
};

const CURVE: LearningCurveResponse = {
  meta: { runs: 3, coupling: { caller: "svc-a@us-east", callee: "svc-b@us-east" } },
  incidents: [
    { incident_id: "inc-041", alert_service: "svc-x@us-east", verdict: "DIAGNOSED", correct: true, tool_calls: 9, steps: 0, ms: 1000, recall_used: false, memory_seeded: false },
    { incident_id: "inc-042", alert_service: "svc-y@us-east", verdict: "DIAGNOSED", correct: true, tool_calls: 5, steps: 0, ms: 900, recall_used: true, memory_seeded: true },
    { incident_id: "inc-043", alert_service: "svc-z@us-east", verdict: "DIAGNOSED", correct: true, tool_calls: 5, steps: 0, ms: 900, recall_used: true, memory_seeded: true },
  ],
};

describe("recommendation narration", () => {
  it("collects recommendations without duplicates and keeps the text in narration", () => {
    const data = { topic: "recommendation", text: REC.banner, recommendations: [REC] };
    const s = reduceAll([ev(0, "narration", data), ev(1, "narration", data)]);
    expect(s.recommendations).toHaveLength(1);
    expect(s.recommendations[0].id).toBe("rec-082");
    expect(s.narration).toContain(REC.banner);
    expect(s.precedent).toEqual([]);
  });
  it("memory events keep the additive write-back fields", () => {
    const s = reduce(initialState, ev(0, "memory", { written: ["x"], detail: "d", cause_id: "chg-1", learned: [{ caller: "a", callee: "b", action: "created", confidence: 0.6, confirmations: 0, contradictions: 0 }] }));
    expect(s.memory?.cause_id).toBe("chg-1");
    expect(s.memory?.learned?.[0].confidence).toBe(0.6);
  });
});

describe("RecommendationBanner", () => {
  it("says which recommendation predicted the incident and for how long it was open", () => {
    expect(recommendationLine(REC)).toBe(REC.banner);
    const html = renderToStaticMarkup(createElement(RecommendationBanner, { recs: [REC] }));
    expect(html).toContain("Predicted by REC-082 (from INC-041 postmortem), open 3 days");
    expect(html).toContain("PREDICTED");
    expect(html).toContain("targets a, b");
  });
  it("uses the singular for one day and renders nothing without recommendations", () => {
    expect(recommendationLine({ ...REC, days_open: 1 })).toContain("open 1 day:");
    expect(renderToStaticMarkup(createElement(RecommendationBanner, { recs: [] }))).toBe("");
  });
});

describe("LearningCurveChart", () => {
  it("draws one bar per incident with first vs second highlighted", () => {
    const html = renderToStaticMarkup(createElement(LearningCurveChart, { data: CURVE }));
    expect(html.match(/data-bar=/g)).toHaveLength(3);
    expect(html).toContain('data-value="9"');
    expect(html).toContain("var(--color-drifted)");
    expect(html).toContain("var(--color-verified)");
    expect(html).toContain("(44% fewer)");
    expect(html).toContain("Tool calls per incident: incident 1 9, incident 2 5, incident 3 5");
    expect(html).toContain("a → b");
  });
  it("draws the learned edge's confidence over the bars, marks contradictions and the recall threshold", () => {
    const row = (id: string, calls: number, conf: number, extra: Partial<LearningCurveIncident> = {}): LearningCurveIncident => ({
      incident_id: id, alert_service: "svc-x@us-east", verdict: "DIAGNOSED", correct: true, tool_calls: calls, steps: 0, ms: 900,
      recall_used: true, memory_seeded: true, edge_confidence: conf, contradicted: false, phase: "learn", ...extra,
    });
    const forget: LearningCurveResponse = {
      meta: { runs: 3, coupling: { caller: "svc-a@us-east", callee: "svc-b@us-east" }, forget: { threshold: 0.25, contradictions: 3 } },
      incidents: [
        row("inc-041", 9, 0.6, { memory_seeded: false }),
        row("inc-042", 5, 0.8),
        row("inc-043", 5, 0.9),
        row("inc-044", 5, 0.45, { contradicted: true, phase: "forget" }),
        row("inc-045", 5, 0.225, { contradicted: true, phase: "forget" }),
        row("inc-046", 9, 0.1125, { contradicted: true, phase: "forget", memory_seeded: false }),
      ],
    };
    const html = renderToStaticMarkup(createElement(LearningCurveChart, { data: forget }));
    expect(html.match(/data-bar=/g)).toHaveLength(6);
    expect(html).toContain("data-confidence-line");
    expect(html.match(/data-confidence=/g)).toHaveLength(6);
    expect(html.match(/data-contradicted="true"/g)).toHaveLength(3);
    expect(html).toContain('data-threshold="0.25"');
    expect(html).toContain("recall threshold 0.25");
    expect(html).toContain("Learned-edge confidence per incident: 0.60, 0.80, 0.90, 0.45, 0.23, 0.11");
    expect(html).toContain("peaked at");
    expect(html).toContain("0.90");
    expect(html).toContain("3 contradicting incidents");
    // the fall below the threshold is called out
    expect(html).toContain("under the recall threshold");
  });
  it("old runs without confidence fields render bars only", () => {
    const html = renderToStaticMarkup(createElement(LearningCurveChart, { data: CURVE }));
    expect(html).not.toContain("data-confidence-line");
    expect(html).not.toContain("data-threshold");
    expect(html).not.toContain("data-contradicted");
  });
  it("handles an empty run", () => {
    const html = renderToStaticMarkup(createElement(LearningCurveChart, { data: { meta: { runs: 0 }, incidents: [] } }));
    expect(html).toContain("no incidents");
  });
});
