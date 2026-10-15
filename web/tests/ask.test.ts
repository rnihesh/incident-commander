import { describe, expect, it } from "vitest";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { Ask, AskAnswer } from "../components/Ask";
import { ASK_MAX, EXAMPLE_QUESTIONS, answerMeta, askStatus, callLabel, checkQuestion, parseAsk, type AskResponse } from "../lib/ask";
import { NetworkError, RateLimitedError, askGraph } from "../lib/api";

const raw = (o: Record<string, unknown> = {}) => ({
  answer: "Payments Core (team-pay) owns it; Ada Park (per-0001) is on call.",
  mode: "llm",
  grounded: true,
  found: true,
  citations: [
    { id: "svc-checkout-api@us-east", kind: "Service", label: "checkout-api" },
    { id: "team-pay", kind: "Team", label: "Payments Core" },
    { id: "per-0001", kind: "Person", label: "Ada Park" },
  ],
  calls: [
    { tool: "find_entities", args: { search: "checkout-api" }, cypher: ["MATCH (n) RETURN n.id"], params: [{ terms: ["checkout-api"] }], nodes_touched: 3, ms: 4.2 },
    { tool: "service_profile", args: { service_id: "svc-checkout-api@us-east" }, cypher: ["MATCH (s:Service {id: $service_id}) RETURN s", "MATCH (c:Change) RETURN c"], params: [{}, {}], nodes_touched: 7, ms: 6 },
  ],
  ms: 1840.4,
  tokens: 2310,
  dropped: [],
  note: null,
  ...o,
});

const parsed = (o: Record<string, unknown> = {}): AskResponse => {
  const r = parseAsk(raw(o));
  if (!r) throw new Error("expected an answer");
  return r;
};

const fetchOf = (payload: unknown, status = 200) => {
  const calls: { url: string; init?: RequestInit }[] = [];
  const f = async (url: string, init?: RequestInit) => {
    calls.push({ url, init });
    return new Response(JSON.stringify(payload), { status });
  };
  return { calls, f };
};

const html = (r: AskResponse, inGraph: (id: string) => boolean = () => false) =>
  renderToStaticMarkup(createElement(AskAnswer, { result: r, inGraph, onFocus: () => {} }));

describe("parseAsk", () => {
  it("reads an answer with its citations and calls", () => {
    const r = parsed();
    expect(r.mode).toBe("llm");
    expect(r.citations.map((c) => c.id)).toEqual(["svc-checkout-api@us-east", "team-pay", "per-0001"]);
    expect(r.calls[1].cypher).toHaveLength(2);
    expect(askStatus(r)).toBe("grounded");
  });

  it("never shows sources for an ungrounded answer and tolerates junk", () => {
    expect(parseAsk(null)).toBeNull();
    expect(parseAsk({ mode: "llm" })).toBeNull();
    const r = parsed({ grounded: false, found: true, citations: [{ id: "x", kind: "Team", label: "X" }], mode: "weird", calls: [{ nope: 1 }, "x"] });
    expect(r.citations).toEqual([]);
    expect(r.mode).toBe("limited");
    expect(r.calls).toEqual([]);
    expect(askStatus(r)).toBe("unverified");
    expect(askStatus(parsed({ grounded: false, found: false, citations: [] }))).toBe("no_info");
  });
});

describe("question helpers", () => {
  it("checks length and emptiness", () => {
    expect(checkQuestion("  ")).toMatch(/Type a question/);
    expect(checkQuestion("x".repeat(ASK_MAX + 1))).toMatch(/500 characters/);
    expect(checkQuestion("Who owns checkout?")).toBeNull();
  });

  it("labels calls and the meta line", () => {
    expect(callLabel({ tool: "find_entities", args: { search: "checkout" } })).toBe('find_entities(search: "checkout")');
    expect(answerMeta(parsed())).toBe("3 queries · 1840 ms · 2,310 tokens");
    expect(answerMeta(parsed({ tokens: 0, calls: [] }))).toBe("0 queries · 1840 ms");
  });

  it("offers four example questions", () => {
    expect(EXAMPLE_QUESTIONS).toHaveLength(4);
  });
});

describe("askGraph", () => {
  it("posts JSON with the question and optional incident", async () => {
    const { calls, f } = fetchOf(raw());
    await askGraph("Who owns checkout?", null, f);
    expect(calls[0].url).toMatch(/\/api\/ask$/);
    expect(calls[0].init?.method).toBe("POST");
    expect(calls[0].init?.credentials).toBe("include");
    expect(JSON.parse(String(calls[0].init?.body))).toEqual({ question: "Who owns checkout?" });
    await askGraph("What happened?", "inc-041", f);
    expect(JSON.parse(String(calls[1].init?.body))).toEqual({ question: "What happened?", incident_id: "inc-041" });
  });

  it("maps 429 to RateLimitedError and 422 to a readable error", async () => {
    const limited = fetchOf({ error: "limit of 20 questions per hour per session reached", reason: "limit of 20 questions per hour per session reached", retry_after: 60 }, 429);
    await expect(askGraph("q", null, limited.f)).rejects.toBeInstanceOf(RateLimitedError);
    const bad = fetchOf({ detail: [{ loc: ["body", "question"], msg: "String should have at most 500 characters" }] }, 422);
    const err = (await askGraph("q", null, bad.f).catch((e: unknown) => e)) as Error;
    expect(err).toBeInstanceOf(NetworkError);
    expect(err.message).toContain("at most 500 characters");
  });
});

describe("AskAnswer", () => {
  it("shows the answer, mode and grounded badges, source chips and the queries", () => {
    const out = html(parsed(), (id) => id === "svc-checkout-api@us-east");
    expect(out).toContain("Ada Park (per-0001) is on call");
    expect(out).toContain("LLM");
    expect(out).toContain("grounded");
    expect(out).toContain("Payments Core");
    expect(out).toContain("Show queries (3)");
    expect(out).toContain("find_entities");
    expect(out).toContain("(s:Service {id: $service_id})"); // the Cypher itself, not a summary
    // only the node on the graph is a pressable chip
    expect(out.match(/aria-pressed/g)).toHaveLength(1);
    expect(out).toContain('title="Focus svc-checkout-api@us-east on the graph"');
  });

  it("shows the limited-mode note", () => {
    const out = html(parsed({ mode: "limited", tokens: 0, note: "Answered in limited mode: no LLM is configured." }));
    expect(out).toContain("limited mode");
    expect(out).toContain("no LLM is configured");
  });

  it("marks an ungrounded answer unverified and lists dropped citations", () => {
    const out = html(parsed({ grounded: false, found: true, citations: [], dropped: ["per-0003"] }));
    expect(out).toContain("unverified");
    expect(out).toContain("per-0003");
    expect(out).not.toContain("Sources");
  });

  it("says when the graph has no information", () => {
    const out = html(parsed({ grounded: false, found: false, citations: [], calls: [], answer: "The graph has no information about that." }));
    expect(out).toContain("no information");
    expect(out).not.toContain("Show queries");
  });
});

describe("Ask panel", () => {
  it("renders the field, the Ask button and the example chips", () => {
    const out = renderToStaticMarkup(createElement(Ask, { incidentId: null, inGraph: () => false, onFocus: () => {} }));
    expect(out).toContain("Ask the graph");
    expect(out).toContain(`maxLength="${ASK_MAX}"`);
    for (const q of EXAMPLE_QUESTIONS) expect(out).toContain(q);
    expect(out).toContain(">Ask<");
  });
});
