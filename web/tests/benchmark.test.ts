import { describe, expect, it } from "vitest";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { BenchmarkTables, approachOrder, honestyNotes, liveRunRows, whyCounts } from "../components/BenchmarkTable";
import { getBenchmark, type BenchWhyRow, type BenchmarkResponse } from "../lib/api";

const stat = (p50: number) => ({ p50_ms: p50, p95_ms: p50 * 2, n: 50 });
const REPORT: BenchmarkResponse = {
  meta: {
    seed: 42, cases: 50, n_seen: 30, n_heldout: 20, commit: "68f8568e1a56d8c1e298fc82512d0a65219d3162",
    classes: ["bad_deploy", "expiry"], heldout_classes: ["expiry"], case_seeds: [1, 2], case_seeds_digest: "53b6b393b51d",
    approaches: [{ name: "graph", label: "graph (deterministic)" }, { name: "vector_rag", label: "vector_rag (hashing embedder, no LLM)" }],
    llm: "none (deterministic/heuristic modes)", notes: ["Correct = exact root entity id."],
  },
  summary: {
    vector_rag: { label: "vector_rag (hashing embedder, no LLM)", n: 50, n_seen: 30, n_heldout: 20, correct: 18, accuracy_seen: 0.3333, accuracy_heldout: 0.4, accuracy_overall: 0.36, mean_ms: 9.9, mean_llm_tokens: 0, mean_prompt_tokens_est: 444, mean_tool_calls: 0 },
    graph: { label: "graph (deterministic)", n: 50, n_seen: 30, n_heldout: 20, correct: 50, accuracy_seen: 1, accuracy_heldout: 1, accuracy_overall: 1, mean_ms: 161.6, mean_llm_tokens: 0, mean_prompt_tokens_est: 0, mean_tool_calls: 9 },
  },
  per_class: {
    graph: { bad_deploy: { n: 10, correct: 10 }, expiry: { n: 10, correct: 10 } },
    vector_rag: { bad_deploy: { n: 10, correct: 0 }, expiry: { n: 10, correct: 4 } },
  },
  latency: {
    world: "seed:world", iterations: 50, counts: { services: 858, depends_on: 1569 },
    ops: { closure: { label: "Upstream closure", falkordb: stat(1.11), postgres: stat(0.89), checked: 50, agreed: 50 } },
  },
  scale: {
    world: "generate_world(42, n_services=4000)", n_services_requested: 4000, seed: 42, closure_depth: 8, iterations: 50,
    counts: { services: 8335, depends_on: 30000 },
    ops: {
      closure_d8: { label: "Upstream closure, depth 8", falkordb: stat(3), postgres: stat(30), checked: 50, agreed: 50, mean_size: 812.4 },
      path: { label: "Shortest path to the farthest reachable service", falkordb: stat(2), postgres: stat(40), checked: 50, agreed: 49, mean_size: 11.2 },
    },
    notes: ["Sources: the 50 deepest of 400."],
  },
  learning_curve: {
    meta: { runs: 2, coupling: { caller: "svc-a@x", callee: "svc-b@x" } },
    incidents: [
      { incident_id: "inc-1", alert_service: "svc-a@x", verdict: "DIAGNOSED", correct: true, tool_calls: 9, steps: 0, ms: 1, recall_used: false, memory_seeded: false },
      { incident_id: "inc-2", alert_service: "svc-a@x", verdict: "DIAGNOSED", correct: true, tool_calls: 5, steps: 0, ms: 1, recall_used: true, memory_seeded: true },
    ],
  },
};

describe("benchmark page", () => {
  it("orders approaches as the report lists them", () => {
    expect(approachOrder(REPORT)).toEqual(["graph", "vector_rag"]);
  });
  it("says plainly that graph accuracy is partly by construction and that no LLM ran", () => {
    const notes = honestyNotes(REPORT).join(" ");
    expect(notes).toMatch(/partly by construction/);
    expect(notes).toMatch(/same causal rule/);
    expect(notes).toMatch(/No LLM key/);
  });
  it("renders every table, the scale section, the learning curve and the footer", () => {
    const h = renderToStaticMarkup(createElement(BenchmarkTables, { data: REPORT }));
    expect(h).toMatch(/data-approach="graph"/);
    expect(h).toMatch(/100%/);
    expect(h).toMatch(/36%/);
    expect(h).toMatch(/\(18\/50\)/);
    expect(h).toMatch(/held-out/);
    expect(h).toMatch(/Upstream closure, depth 8/);
    expect(h).toMatch(/49\/50/);
    expect(h).toMatch(/812\.4/);
    expect(h).toMatch(/8,335 services/);
    expect(h).toMatch(/data-bar="2"/);
    expect(h).toMatch(/seed 42/);
    expect(h).toMatch(/68f8568e1a56/);
    expect(h).toMatch(/50 cases/);
    expect(h).toMatch(/partly by construction/);
  });
  const LIVE = [
    {
      model: "gpt-6-luna", cases: 50, n_seen: 30, n_heldout: 20, commit: "76921df63c47e08bf9a8422b487081bca1d7180c",
      summary: {
        graph: { correct: 50, n: 50, accuracy_overall: 1, accuracy_heldout: 1, mean_llm_tokens: 22393.3, fallback: 0 },
        vector_rag: { correct: 4, n: 50, accuracy_overall: 0.08, accuracy_heldout: 0.15, mean_llm_tokens: 569.9 },
      },
    },
  ];
  it("maps live runs to one row per approach and points the no-LLM note at them", () => {
    const rows = liveRunRows(LIVE);
    expect(rows.map((r) => [r.approach, r.first, r.correct])).toEqual([["graph", true, 50], ["vector_rag", false, 4]]);
    expect(rows.map((r) => r.fallback)).toEqual([0, null]);
    const withLive = { ...REPORT, live_llm_runs: LIVE };
    expect(honestyNotes(withLive).join(" ")).toMatch(/headline deterministic table above has no LLM/);
    expect(honestyNotes(withLive).join(" ")).not.toMatch(/No LLM key/);
    const h = renderToStaticMarkup(createElement(BenchmarkTables, { data: withLive }));
    expect(h).toMatch(/Live LLM runs/);
    expect(h).toMatch(/data-live-run="gpt-6-luna"/);
    expect(h).toMatch(/\(50\/50\)/);
    expect(h).toMatch(/22,393/);
    expect(renderToStaticMarkup(createElement(BenchmarkTables, { data: REPORT }))).not.toMatch(/Live LLM runs/);
  });
  const why = (approach: string, verdict: string, clause: string | null, predicted: string | null): BenchWhyRow => ({
    case: "c000", fault_class: "undeclared_coupling", held_out: true, approach, predicted, verdict, clause,
    detail: clause ? "deploy cannot produce saturation" : null,
    query_text: clause ? "MATCH (c:Change {id: $entity_id}) RETURN 'signature' AS failed_clause" : null,
  });
  const WHY = [
    why("full_context", "REFUTED", "signature", "chg-05692"),
    why("full_context", "REFUTED", "after_onset", "chg-05691"),
    why("full_context", "REFUTED", "signature", "chg-05689"),
    why("vector_rag", "no_claim", null, null),
    why("vector_rag", "cannot_regenerate", null, "chg-05688"),
  ];
  it("counts why-wrong rows per approach by clause, funnel order first", () => {
    expect(whyCounts(WHY)).toEqual([
      { approach: "full_context", total: 3, counts: [["after_onset", 1], ["signature", 2]] },
      { approach: "vector_rag", total: 2, counts: [["cannot_regenerate", 1], ["no_claim", 1]] },
    ]);
  });
  it("renders the why-it-was-wrong table with clause and query, hidden when absent", () => {
    const live = [{ ...LIVE[0], model: "claude-opus-5-5", cases: 10, why_wrong: WHY }, LIVE[0]];
    const h = renderToStaticMarkup(createElement(BenchmarkTables, { data: { ...REPORT, live_llm_runs: live } }));
    expect(h).toMatch(/Why it was wrong/);
    expect(h).toMatch(/data-why-run="claude-opus-5-5"/);
    expect(h).not.toMatch(/data-why-run="gpt-6-luna"/);
    expect(h).toMatch(/data-why-chip="signature"[^>]*>[\s\S]*?signature[\s\S]*?2/);
    expect(h).toMatch(/after onset/);
    expect(h).toMatch(/no claim/);
    expect(h).toMatch(/cannot regenerate/);
    expect(h).toMatch(/chg-05692/);
    expect(h).toMatch(/deploy cannot produce saturation/);
    expect(h).toMatch(/failed_clause/);
    expect(h).toMatch(/undeclared_coupling/);
    const plain = renderToStaticMarkup(createElement(BenchmarkTables, { data: { ...REPORT, live_llm_runs: LIVE } }));
    expect(plain).not.toMatch(/Why it was wrong/);
    const empty = [{ ...LIVE[0], why_wrong: [] }];
    expect(renderToStaticMarkup(createElement(BenchmarkTables, { data: { ...REPORT, live_llm_runs: empty } }))).not.toMatch(/Why it was wrong/);
  });
  it("shows a placeholder when the scale section is missing", () => {
    const h = renderToStaticMarkup(createElement(BenchmarkTables, { data: { ...REPORT, scale: null, latency: { ...REPORT.latency!, error: "Postgres unavailable" } } }));
    expect(h).toMatch(/ic bench --scale N/);
    expect(h).toMatch(/Not measured: Postgres unavailable/);
  });
  it("fetches GET /api/benchmark without a body", async () => {
    let seen: { url: string; init?: RequestInit } | null = null;
    const f = async (url: string, init?: RequestInit) => {
      seen = { url, init };
      return new Response(JSON.stringify(REPORT), { status: 200, headers: { "Content-Type": "application/json" } });
    };
    const r = await getBenchmark(f);
    expect(r.meta.cases).toBe(50);
    expect(seen!.url).toMatch(/\/api\/benchmark$/);
    expect(seen!.init?.method ?? "GET").toBe("GET");
  });
});
