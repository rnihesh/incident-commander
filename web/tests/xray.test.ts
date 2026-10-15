import { describe, expect, it } from "vitest";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { NetworkError, RetryableError, xrayStep, type XrayOp, type XrayResponse } from "../lib/api";
import { XRAY_NOTE, findOperatorPath, naiveHighlight, proofSeq, xrayRows } from "../lib/xray";
import { Cypher, XrayPlan } from "../components/ui";

const op = (name: string, args: string | null, records: number | null, ms: number | null, children: XrayOp[] = []): XrayOp => ({
  name, args, records, ms, children,
});
const plan = (stats: boolean): XrayOp => {
  const s = (r: number, m: number) => (stats ? [r, m] : [null, null]) as [number | null, number | null];
  return op("Project", null, ...s(4, 0.0019), [
    op("Filter", null, ...s(4, 0.0059), [
      op("Conditional Variable Length Traverse", "(u)-[:IMPACTS*1..4]->(s)", ...s(4, 0.033), [
        op("Node By Label Scan", "(s:Service)", ...s(858, 1.25)),
      ]),
    ]),
  ]);
};
const RESP: XrayResponse = {
  incident_id: "inc-041",
  step: { funnel: "origin" },
  graph: "s:abc:asof:inc-041:known",
  cypher: "MATCH (c:Change) RETURN c",
  params: { iid: "inc-041" },
  explain: plan(false),
  profile: plan(true),
};
const json = (status: number, body: unknown) => async () =>
  new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });

describe("xray api", () => {
  it("posts only the step id, never Cypher", async () => {
    let url = "";
    let init: RequestInit | undefined;
    const f = async (u: string, i?: RequestInit) => {
      url = u;
      init = i;
      return json(200, RESP)();
    };
    const r = await xrayStep("inc-041", { proof: 3 }, f);
    expect(r.graph).toBe(RESP.graph);
    expect(url).toMatch(/\/api\/incidents\/inc-041\/xray$/);
    expect(init?.method).toBe("POST");
    expect(JSON.parse(String(init?.body))).toEqual({ proof: 3 });
    expect(init?.credentials).toBe("include");
  });
  it("maps 404 and 504 to NetworkError with the server's message", async () => {
    const e404 = await xrayStep("inc-041", { funnel: "bogus" }, json(404, { detail: "unknown funnel clause 'bogus'" })).catch((x) => x);
    expect(e404).toBeInstanceOf(NetworkError);
    expect(e404.status).toBe(404);
    expect(e404.message).toContain("unknown funnel clause");
    const e504 = await xrayStep("inc-041", { funnel: "origin" }, json(504, { error: "GRAPH.PROFILE timed out after 5000 ms" })).catch((x) => x);
    expect(e504).toBeInstanceOf(NetworkError);
    expect(e504.status).toBe(504);
  });
  it("retries while snapshots are still building", async () => {
    let n = 0;
    const f = async () => (++n < 2 ? json(409, { error: "building", retry: true })() : json(200, RESP)());
    const r = await xrayStep("inc-041", { funnel: "origin" }, f, { sleep: async () => {} });
    expect(r.incident_id).toBe("inc-041");
    expect(n).toBe(2);
    const e = await xrayStep("inc-041", { funnel: "origin" }, json(409, { retry: true }), { attempts: 1 }).catch((x) => x);
    expect(e).toBeInstanceOf(RetryableError);
  });
});

describe("xray helpers", () => {
  it("parses the ledger seq from a hypothesis or proof id", () => {
    expect(proofSeq("hyp-inc-041-3")).toBe(3);
    expect(proofSeq("prf-inc_9-12")).toBe(12);
    expect(proofSeq("hyp-1")).toBe(1);
    expect(proofSeq("nonsense")).toBeNull();
  });
  it("flattens the plan pre-order with depth and merges profile stats", () => {
    const rows = xrayRows(RESP.explain, RESP.profile);
    expect(rows.map((r) => [r.depth, r.name])).toEqual([
      [0, "Project"],
      [1, "Filter"],
      [2, "Conditional Variable Length Traverse"],
      [3, "Node By Label Scan"],
    ]);
    expect(rows[3]).toMatchObject({ args: "(s:Service)", records: 858, ms: 1.25 });
  });
  it("falls back to the profile tree when the shapes differ", () => {
    const rows = xrayRows(op("Results", null, null, null), RESP.profile);
    expect(rows[0].name).toBe("Project");
    expect(rows[0].records).toBe(4);
  });
});

describe("naive plan highlighting", () => {
  it("finds the operator path (child indices) of the first match", () => {
    const t = plan(false);
    expect(findOperatorPath(t, (o) => o.name === "Project")).toEqual([]);
    expect(findOperatorPath(t, (o) => o.name === "Node By Label Scan")).toEqual([0, 0, 0]);
    expect(findOperatorPath(t, (o) => o.name === "Nope")).toBeNull();
  });
  it("marks the Filter above the Conditional Variable Length Traverse by pre-order row", () => {
    expect(naiveHighlight(plan(false))).toEqual({ filter: 1, traverse: 2 });
  });
  it("picks the nearest Filter above the traverse, and none when there is no traverse or no Filter", () => {
    const nested = op("Project", null, null, null, [
      op("Filter", null, null, null, [op("Filter", null, null, null, [op("Conditional Variable Length Traverse", null, null, null)])]),
    ]);
    expect(naiveHighlight(nested)).toEqual({ filter: 2, traverse: 3 });
    expect(naiveHighlight(op("Project", null, null, null, [op("Node By Label Scan", null, null, null)]))).toBeNull();
    expect(naiveHighlight(op("Project", null, null, null, [op("Conditional Variable Length Traverse", null, null, null)]))).toEqual({ filter: null, traverse: 1 });
  });
});

describe("xray rendering", () => {
  const WITH_NAIVE: XrayResponse = {
    ...RESP,
    graph: "as-of snapshot of inc-041",
    naive: { cypher: "MATCH p=(u)-[:IMPACTS*1..4]->(s) WHERE all(e IN relationships(p) WHERE e.valid_from <= $t) RETURN p", graph_label: "full world", explain: plan(false) },
  };
  it("shows the naive plan beside the snapshot plan when the server sends one", () => {
    const html = renderToStaticMarkup(createElement(XrayPlan, { data: WITH_NAIVE }));
    expect(html).toContain("What we avoid: valid-time filter inside a variable-length traversal");
    expect(html).toContain("What we run: on the as-of snapshot");
    expect(html).toContain("full world");
    expect(html).toContain("e.valid_from");
    expect(html).toContain('data-naive-hit="filter"');
    expect(html).toContain('data-naive-hit="traverse"');
    expect(html).toContain("858");
  });
  it("renders the single plan for older responses without naive", () => {
    const html = renderToStaticMarkup(createElement(XrayPlan, { data: RESP }));
    expect(html).not.toContain("What we avoid");
    expect(html).toContain("1.25 ms");
  });
  it("shows operator names, records, ms and the design note", () => {
    const html = renderToStaticMarkup(createElement(XrayPlan, { data: RESP }));
    for (const name of ["Project", "Filter", "Conditional Variable Length Traverse", "Node By Label Scan"]) expect(html).toContain(name);
    expect(html).toContain("858");
    expect(html).toContain("1.25 ms");
    expect(html).toContain("(u)-[:IMPACTS*1..4]-&gt;(s)");
    expect(html).toContain("s:abc:asof:inc-041:known");
    expect(XRAY_NOTE).toMatch(/after the traversal/);
    expect(XRAY_NOTE).toMatch(/as-of snapshot/);
    expect(html).toContain("as-of snapshot");
  });
  it("offers the X-ray toggle only when the query can be re-derived", () => {
    const off = renderToStaticMarkup(createElement(Cypher, { text: "MATCH (n) RETURN n" }));
    expect(off).not.toContain("X-ray");
    const on = renderToStaticMarkup(createElement(Cypher, { text: "MATCH (n) RETURN n", xray: async () => RESP }));
    expect(on).toContain("X-ray");
    expect(on).toContain('aria-expanded="false"');
  });
});
