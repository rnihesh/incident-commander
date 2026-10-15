// Judge challenge (R5): the live answer from POST /api/incidents/{id}/challenge, read
// defensively, plus the plain-English labels the panel shows for each failing clause.
import type { CertificateData, Clause, VerdictData } from "./payloads";

export type ChallengeVerdict = "VERIFIED" | "RIVAL" | "REFUTED";
export type ChallengeClause = Clause | "outside_window" | "not_a_change";

/** Raw response body; the backend spreads verify_claim's result into it. */
export interface ChallengeResponse {
  incident_id: string;
  change_id: string;
  is_root: boolean;
  diagnosed_root: string | null;
  verdict: ChallengeVerdict | null;
  clause?: ChallengeClause | null;
  detail?: string;
  query_text?: string;
  rivals?: { id: string; service_id?: string | null }[];
  claim?: { service_id?: string | null; event_time?: number | null } | null;
  would_overturn: "INCONCLUSIVE" | null;
  message?: string;
  limit: number;
  remaining: number;
}

export interface ChallengeBudget {
  incident_id: string;
  limit: number;
  remaining: number;
}

export type ChallengeResult =
  | { kind: "root"; change: string; message: string; remaining: number; limit: number }
  | {
      kind: "verdict";
      change: string;
      verdict: ChallengeVerdict;
      clause: ChallengeClause | null;
      detail: string;
      queryText: string;
      service: string | null;
      rivals: string[];
      wouldOverturn: boolean;
      remaining: number;
      limit: number;
    };

/** What a failing clause means, in a sentence a judge can read without the predicate. */
export const CLAUSE_LABELS: Record<ChallengeClause, string> = {
  after_onset: "It landed after the first symptom, so it cannot have caused it.",
  origin: "Its service was not the first one to fail.",
  coverage: "Its failure cannot reach every service that broke.",
  order: "The symptoms appeared in an order its dependency path cannot produce.",
  signature: "This kind of change cannot produce the errors that were seen.",
  outside_window: "It happened outside the incident's alert window.",
  not_a_change: "It names a service; a root cause is a change or an expiry.",
};

const CLAUSE_NAMES = Object.keys(CLAUSE_LABELS) as ChallengeClause[];
const VERDICTS: ChallengeVerdict[] = ["VERIFIED", "RIVAL", "REFUTED"];

/** Short clause name for badges: "after_onset" -> "after onset". */
export const clauseName = (c: ChallengeClause) => c.replace(/_/g, " ");

export function clauseLabel(c: string | null | undefined): string | null {
  return c && (CLAUSE_NAMES as string[]).includes(c) ? CLAUSE_LABELS[c as ChallengeClause] : null;
}

/** Badge text: "REFUTED · after onset", "QUALIFIES · rival of the root", "QUALIFIES · alone". */
export function verdictLabel(r: Extract<ChallengeResult, { kind: "verdict" }>): string {
  if (r.verdict === "REFUTED") return r.clause ? `REFUTED · ${clauseName(r.clause)}` : "REFUTED";
  return r.verdict === "RIVAL" ? "QUALIFIES · alongside another change" : "QUALIFIES · on its own";
}

const rec = (v: unknown): Record<string, unknown> | null =>
  v && typeof v === "object" && !Array.isArray(v) ? (v as Record<string, unknown>) : null;
const str = (v: unknown): string | undefined => (typeof v === "string" && v ? v : undefined);
const num = (v: unknown, d: number): number => (typeof v === "number" && Number.isFinite(v) ? v : d);

/** The panel's view of a challenge response; null when it is not recognisable. */
export function parseChallenge(body: unknown): ChallengeResult | null {
  const r = rec(body);
  const change = str(r?.change_id) ?? str(r?.entity_id);
  if (!r || !change) return null;
  const remaining = num(r.remaining, 0);
  const limit = num(r.limit, 2);
  if (r.is_root === true) {
    return { kind: "root", change, message: str(r.message) ?? `${change} is the diagnosed root.`, remaining, limit };
  }
  const verdict = VERDICTS.find((v) => v === r.verdict);
  if (!verdict) return null;
  const clause = (CLAUSE_NAMES as string[]).includes(str(r.clause) ?? "") ? (r.clause as ChallengeClause) : null;
  const rivals = Array.isArray(r.rivals) ? r.rivals.map((x) => str(rec(x)?.id) ?? str(x)).filter((x): x is string => !!x) : [];
  return {
    kind: "verdict",
    change,
    verdict,
    clause: verdict === "REFUTED" ? clause : null,
    detail: str(r.detail) ?? "",
    queryText: str(r.query_text) ?? "",
    service: str(rec(r.claim)?.service_id) ?? null,
    rivals,
    wouldOverturn: r.would_overturn === "INCONCLUSIVE",
    remaining,
    limit,
  };
}

/** The changes a judge may pick: the diagnosed root first (answered free), then every change
 * the certificate eliminated, newest first. Empty until the verdict and certificate exist. */
export function challengeOptions(cert: CertificateData | null, verdict: VerdictData | null): { change: string; service: string; root: boolean }[] {
  if (!cert) return [];
  const root = verdict?.root_cause;
  const seen = new Set<string>(root ? [root.change] : []);
  const eliminated = [...cert.checked]
    .reverse()
    .filter((c) => !seen.has(c.change) && (seen.add(c.change), true))
    .map((c) => ({ change: c.change, service: c.service, root: false }));
  return root ? [{ change: root.change, service: root.service, root: true }, ...eliminated] : eliminated;
}

/** A budget GET that resolves after ask() wrote a fresher one for the same incident must not overwrite it. */
export const acceptFetchedBudget = (writtenFor: string | null, incident: string): boolean => writtenFor !== incident;
