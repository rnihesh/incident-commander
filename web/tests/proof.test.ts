import { describe, expect, it } from "vitest";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { ProofView, proofQueryText } from "../components/ProofPage";
import { NetworkError, RateLimitedError, getProof, isProofRef, reverifyProof, type ProofPageResponse, type ProofVerifyResponse } from "../lib/api";

const REF = "0123456789abcdef01234567";
const json = (status: number, body: unknown) => async () =>
  new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });

const PAGE: ProofPageResponse = {
  ref: REF,
  incident_id: "inc-0042",
  title: "Checkout latency spike",
  alert_service: "svc-checkout@us-east",
  alert_time: "2026-10-01T12:00:00Z",
  verdict: "DIAGNOSED",
  suspect: "chg-00017",
  chain_head: "ab".repeat(32),
  anchored: true,
  anchor_matches: true,
  proofs: [
    {
      seq: 1, proof_id: "prf-inc-0042-1", hypothesis_id: "hyp-inc-0042-1", kind: "suspect_qualifies", entity_id: "chg-00017",
      claim: "chg-00017 explains every symptom", state: "HOLDS", holds: true, conclusion: { ok: true }, result_hash: "r1",
      hash: "11".repeat(32), prev_hash: null, cypher: ["MATCH (c:Change {id: $id}) RETURN c", "MATCH (s:Symptom) RETURN s"],
      params: [{ id: "chg-00017" }, {}],
    },
    {
      seq: 2, proof_id: "prf-inc-0042-2", hypothesis_id: "hyp-inc-0042-2", kind: "entity_refuted", entity_id: "chg-00020",
      claim: "chg-00020 fails the order clause", state: "REFUTED", holds: false, conclusion: { ok: false }, result_hash: "r2",
      hash: "22".repeat(32), prev_hash: "11".repeat(32), cypher: ["MATCH (c:Change {id: $id}) RETURN c"], params: [{ id: "chg-00020" }],
    },
  ],
  chain_error: null,
  rehearsal: { result: "CLEARED", change_id: "chg-00017", checked: 4, detail: "cleared", clone_ms: 80 },
  pr: { url: "https://github.com/o/r/pull/1234", number: 1234, simulated: true, opened_at: "2026-10-01T12:05:00Z", expires_at: "2026-10-01T13:05:00Z" },
  status_context: "incident-commander/proofs",
  last_verify: null,
};

const verify = (status: ProofVerifyResponse["status"], extra: Partial<ProofVerifyResponse> = {}): ProofVerifyResponse => ({
  incident_id: "inc-0042", status, chain_ok: status !== "TAMPERED", broken_at: null, anchor_ok: true,
  stored_head: PAGE.chain_head, recomputed_head: PAGE.chain_head, anchored_head: PAGE.chain_head,
  proofs: PAGE.proofs.map((p) => ({
    seq: p.seq, proof_id: p.proof_id, kind: p.kind, entity_id: p.entity_id, claim: p.claim, recorded_state: p.state,
    status: status === "FALSIFIED" && p.seq === 2 ? "FALSIFIED" : "VERIFIED", responsible: status === "FALSIFIED" && p.seq === 2 ? ["chg-judge-1"] : [],
    late_arriving: false, late_ids: [], hash_matches: true, error: null, tampered_reason: null,
  })),
  tampered_reasons: [], ms: 812, verified_at: "2026-10-01T12:10:00Z", cached: false,
  commit_status: { posted: true, state: status === "VERIFIED" ? "success" : "failure", simulated: true },
  ...extra,
});

const render = (v: ProofVerifyResponse | null, err: string | null = null) =>
  renderToStaticMarkup(createElement(ProofView, { data: PAGE, verify: v, running: false, verifyError: err, onVerify: () => {} }));

describe("proof page", () => {
  it("validates proof refs like the backend", () => {
    expect(isProofRef(REF)).toBe(true);
    expect(isProofRef(REF.toUpperCase())).toBe(false);
    expect(isProofRef("../etc/passwd")).toBe(false);
  });

  it("renders the header, chain and a re-verify button with no result yet", () => {
    const html = render(null);
    expect(html).toContain("Checkout latency spike");
    expect(html).toContain("DIAGNOSED");
    expect(html).toContain("ANCHORED");
    expect(html).toContain("chg-00017");
    expect(html).toContain("CLEARED");
    expect(html).toContain("PR #1234 (simulated)");
    expect(html).toContain("Re-verify");
    expect(html).toContain("Show queries (2)");
    expect(html).toContain("Copy query");
    expect(html).toContain("Not re-verified yet");
  });

  it.each(["VERIFIED", "DRIFTED", "FALSIFIED", "TAMPERED", "UNANCHORED"] as const)("shows %s after a re-verify", (s) => {
    const html = render(verify(s, s === "TAMPERED" ? { tampered_reasons: ["chain broken at seq 2"], anchor_ok: false } : {}));
    expect(html).toContain(`>${s}<`);
    if (s === "FALSIFIED") expect(html).toContain("chg-judge-1");
    if (s === "TAMPERED") expect(html).toContain("chain broken at seq 2");
  });

  it("labels a cached (debounced) result and shows rate-limit errors", () => {
    expect(render(verify("VERIFIED", { cached: true }))).toContain("cached");
    expect(render(null, "too many re-verifies")).toContain("too many re-verifies");
  });

  it("copies every query of a proof with its params", () => {
    const text = proofQueryText(PAGE.proofs[0]);
    expect(text).toContain("MATCH (c:Change {id: $id}) RETURN c");
    expect(text).toContain('// params {"id":"chg-00017"}');
    expect(text).toContain("MATCH (s:Symptom) RETURN s");
  });

  it("calls the public endpoints and maps 404 / 429", async () => {
    let url = "";
    let init: RequestInit | undefined;
    await getProof(REF, async (u, i) => { url = u; init = i; return new Response(JSON.stringify(PAGE), { status: 200 }); });
    expect(url).toMatch(new RegExp(`/api/proof/${REF}$`));
    expect(init?.method).toBeUndefined();
    await reverifyProof(REF, async (u, i) => { url = u; init = i; return new Response(JSON.stringify(verify("VERIFIED")), { status: 200 }); });
    expect(url).toMatch(new RegExp(`/api/proof/${REF}/verify$`));
    expect(init?.method).toBe("POST");
    expect((init?.headers as Record<string, string>)["Content-Type"]).toBe("application/json");
    const nf = await getProof(REF, json(404, { detail: "unknown proof" })).catch((x) => x);
    expect(nf).toBeInstanceOf(NetworkError);
    expect(nf.status).toBe(404);
    const rl = await reverifyProof(REF, json(429, { error: "slow down", reason: "slow down" })).catch((x) => x);
    expect(rl).toBeInstanceOf(RateLimitedError);
    expect(rl.reason).toBe("slow down");
  });
});
