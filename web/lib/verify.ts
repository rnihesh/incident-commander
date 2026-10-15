import type { VerifyResponse } from "./api";
import type { VerifyResult } from "./mock/verify";
import type { ProofEntry } from "./reducer";
import type { VerifyState } from "./payloads";

const STATES: VerifyState[] = ["VERIFIED", "DRIFTED", "UNANCHORED", "FALSIFIED", "TAMPERED"];
const asState = (s: string): VerifyState | null => {
  const u = s.toUpperCase();
  return (STATES as string[]).includes(u) ? (u as VerifyState) : null;
};

export interface ChainVerify {
  result: VerifyResult;
  chain_ok: boolean;
  anchor_ok: boolean;
}

/** Map the live /verify response onto the per-proof results the chain UI renders. */
export function mapVerify(resp: VerifyResponse, proofs: ProofEntry[]): { byHypothesis: Record<string, VerifyResult>; chain: ChainVerify } {
  const byHypothesis: Record<string, VerifyResult> = {};
  proofs.forEach((p) => {
    // Match by chain seq, then entity + kind; never by array index.
    const hit =
      resp.proofs.find((x) => x.seq === p.position) ??
      resp.proofs.find((x) => x.entity_id === p.hypothesis_id && x.kind === "hypothesis");
    if (!hit) return; // unmatched: leave without a state rather than claiming VERIFIED
    // Per-proof state is never UNANCHORED: that is a chain-level fact.
    let state = asState(hit.status) || "VERIFIED";
    if (state === "UNANCHORED") state = "VERIFIED";
    if (resp.overall === "TAMPERED" && hit.tampered_reason) state = "TAMPERED";
    const node = hit.responsible?.[0];
    byHypothesis[p.hypothesis_id] = {
      state,
      node,
      detail:
        hit.tampered_reason ??
        (state === "VERIFIED" ? "Result hash matches." : `Re-run status ${hit.status}.`),
      recomputed: p.hash,
      reasons: hit.tampered_reason ? [hit.tampered_reason] : undefined,
    };
  });
  const detail: Record<VerifyResponse["overall"], string> = {
    VERIFIED: "Chain and anchor verified; every proof re-ran to the same result.",
    DRIFTED: "A proof result changed but its conclusion still holds.",
    UNANCHORED: "Chain is intact but has not been anchored yet.",
    FALSIFIED: "A refuted suspect now qualifies.",
    TAMPERED: "A stored proof or the chain was modified.",
  };
  return {
    byHypothesis,
    chain: {
      chain_ok: resp.chain_ok,
      anchor_ok: resp.anchor_ok,
      result: {
        state: resp.overall,
        detail: detail[resp.overall] ?? resp.overall,
        recomputed: "",
        reasons: resp.tampered_reasons?.length ? resp.tampered_reasons : undefined,
      },
    },
  };
}
