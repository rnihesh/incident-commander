import type { ProofEntry } from "../reducer";
import type { VerifyState } from "../payloads";
import { fakeHash } from "./builders";
import { HERO_FALSIFIER_NODE } from "./hero-graph";

export type VerifyMode = "ok" | "drifted" | "falsified" | "tampered";

export interface VerifyResult {
  state: VerifyState;
  /** Node or record responsible for the change, named in the UI. */
  node?: string;
  detail: string;
  recomputed: string;
  /** TAMPERED: reasons reported by the backend. */
  reasons?: string[];
}

/** Mocked Verify. The real one re-runs each proof's query against the current world (KTD4). */
export function mockVerify(p: ProofEntry, mode: VerifyMode, isHead: boolean): VerifyResult {
  if (mode === "tampered" && isHead) {
    return {
      state: "TAMPERED",
      node: p.hash.slice(0, 12),
      detail: "Recomputed chain head differs from the anchored head. A stored proof was rewritten.",
      recomputed: fakeHash(`tampered|${p.hash}`),
    };
  }
  if (mode === "falsified" && p.state === "REFUTED") {
    return {
      state: "FALSIFIED",
      node: HERO_FALSIFIER_NODE,
      detail: `A refuted suspect now qualifies: ${HERO_FALSIFIER_NODE} gained an IMPACTS edge after the alert.`,
      recomputed: fakeHash(`falsified|${p.hash}`),
    };
  }
  if (mode === "drifted" && p.position === 1) {
    return {
      state: "DRIFTED",
      node: "svc-quote-engine@us-east",
      detail: "Result changed (hop count 1 to 2) but the conclusion still holds.",
      recomputed: fakeHash(`drift|${p.hash}`),
    };
  }
  return { state: "VERIFIED", detail: "Result hash matches.", recomputed: p.hash };
}
