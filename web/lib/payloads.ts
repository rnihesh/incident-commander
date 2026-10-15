// Payload shapes the console expects inside RunEvent.data. These are the UI's
// reading of the contract in events.ts; the backend stream (U9) must emit them.
// Everything optional is genuinely optional: the reducer tolerates absence.

export type IncidentState =
  | "OPEN"
  | "TRIAGING"
  | "DIAGNOSED"
  | "INCONCLUSIVE"
  | "FAILED"
  | "REHEARSING"
  | "REFUSED"
  | "ACTING"
  | "PR_OPEN"
  | "RESOLVED"
  | "LEARNED";

export type Clause = "after_onset" | "origin" | "coverage" | "order" | "signature";
export const CLAUSES: Clause[] = ["after_onset", "origin", "coverage", "order", "signature"];

export interface RunStartedData {
  incident_id: string;
  title: string;
  scenario?: string;
  recorded?: boolean;
  alert: {
    service: string;
    signal: string;
    severity: string;
    fired_at: string; // ISO, frozen world clock
    detail?: string;
  };
}

export interface SuspectRef {
  change: string;
  service: string;
  summary: string;
}

export interface ReflexData {
  ms: number;
  label: "pre-snapshot";
  /** Services showing symptoms. */
  blast_radius?: number;
  /** Services upstream of the alert, where the cause must be. */
  search_space?: number;
  suspects: SuspectRef[];
  oncall: { name: string; team?: string; channel?: string };
}

export interface SnapshotData {
  build_ms: number;
  graphs: { name: "true" | "known"; nodes: number; edges: number }[];
  symptomatic: string[];
  /** Services with a Change/Expiry in the incident window, on the impact area, recorded after t. */
  late_arriving?: string[];
  /** DEPENDS_ON edges valid at t, on the impact area, recorded after t. */
  late_edges?: LateEdge[];
}

export interface LateEdge {
  caller: string;
  callee: string;
}

export interface HypothesisData {
  id: string;
  suspect: string;
  statement: string;
  falsifier_tool: string;
  expected: string;
  state: "REGISTERED";
}

export type ProofState = "HOLDS" | "REFUTED";
export interface ProofData {
  hypothesis_id: string;
  position: number; // chain position, 1-based
  hash: string;
  prev_hash: string | null;
  state: ProofState;
  summary: string;
  query: string;
  params?: Record<string, unknown>;
}

export interface EliminatedChange {
  change: string;
  service: string;
  clause: Clause;
  late?: boolean;
  event_time?: string; // ISO; absent on older payloads
}

export interface FunnelData {
  step: number;
  clause: Clause;
  label: string;
  before: number;
  after: number;
  cypher: string;
  params?: Record<string, unknown>;
  eliminated: EliminatedChange[];
  top_suspects: string[];
}

export interface CertificateData {
  services_checked: number;
  window: { from: string; to: string };
  qualifying: number; // rivals that qualify; 0 for a clean certificate
  suspect: string;
  /** The clause the suspect itself fails (an INCONCLUSIVE run); null/absent when it passes all. */
  suspect_clause?: Clause | null;
  checked: EliminatedChange[];
  onset?: string; // ISO earliest symptom onset; absent on older payloads
  suspect_event_time?: string;
  hash: string;
}

export interface PathRef {
  label: string;
  change: string;
  service: string;
  nodes: string[]; // service ids, cause first
}

export type DegradedReason = "no_llm" | "step_cap" | "spend_cap" | "llm_error" | "timeout";

export interface VerdictData {
  outcome: "DIAGNOSED" | "INCONCLUSIVE" | "FAILED";
  degraded?: boolean;
  degraded_reason?: DegradedReason | string;
  truncated?: boolean; // the summary was cut at its length cap (it ends with an ellipsis)
  headline: string;
  root_cause?: {
    change: string;
    service: string;
    kind: string;
    summary: string;
    author?: string;
    deployed_at?: string;
    known_at_alert: boolean; // recorded_at <= alert time
    lead_s?: number; // earliest symptom onset - event time, seconds (as true)
    earliest_onset?: string;
  };
  paths: PathRef[]; // 1 for DIAGNOSED, 2+ for INCONCLUSIVE
  chain_head?: string;
  confidence_note?: string;
  // Additive (U13): effort counters and what memory contributed.
  steps?: number; // LLM turns (0 for the deterministic fallback)
  tool_calls?: number; // registry tool calls, the verdict's causal path included
  memory?: VerdictMemory;
}

export interface VerdictMemory {
  recalled: boolean;
  used: boolean; // recall offered at least one seed
  seeded: boolean; // the verdict's suspect was a memory seed
  seed_path?: boolean; // concluded on the seed without screening (every proof still ran)
  seeds: string[];
  learned: { caller: string; callee: string; confidence: number }[];
}

export interface StateData {
  state: IncidentState;
}

export interface HandoffData {
  to: string;
  summary: string;
  channel?: string;
}

export interface StillExplains {
  entity_id: string;
  full: boolean; // qualifies for the whole symptom set, not just part of it
  services: string[]; // symptomatic services it still explains
  path: string[];
}

export interface RehearsalData {
  result: "CLEARED" | "REFUSED" | "SKIPPED";
  ms?: number;
  detail: string;
  surviving?: { change: string; path: string[] };
  change?: string; // the rolled-back change
  checked?: number; // remaining candidates re-checked on the clone
  still_explains?: StillExplains[];
}

export interface ActionData {
  kind: "pr" | "commit_status" | "page";
  status: "ok" | "failed";
  label: string;
  url?: string;
  detail?: string;
}

export interface MemoryData {
  written: string[];
  detail: string;
  // Additive (U13 write-back).
  incident_id?: string;
  cause_id?: string;
  trigger?: "resolved" | "reveal" | string;
  rollback_change?: string | null;
  postmortem_id?: string | null;
  recommendations?: string[];
  learned?: LearnedEdge[];
}

export interface LearnedEdge {
  caller: string;
  callee: string;
  action: "created" | "confirmed" | "contradicted";
  confidence: number;
  confirmations: number;
  contradictions: number;
  first_learned_incident?: string;
}

/** An open postmortem recommendation targeting the causal path (R23). */
export interface RecommendationRef {
  id: string;
  display_id: string;
  text: string;
  days_open: number;
  opened_at?: number;
  incident_id?: string | null;
  postmortem_id?: string | null;
  services: string[];
  banner: string;
}

export interface PrecedentMatch {
  incident: string;
  title: string;
  method: "semantic" | "fuzzy" | "path_shape";
  score: number;
  cause: string;
  // Additive: present for matches built from a `precedents` payload.
  postmortem_id?: string;
  methods?: Partial<Record<"vector" | "fulltext" | "fingerprint", number>>;
  remediation?: string;
  resolved_minutes?: number;
}

/** One precedent as emitted by the backend after the verdict. */
export interface PrecedentRecord {
  incident_id: string;
  postmortem_id?: string;
  title: string;
  methods?: Partial<Record<"vector" | "fulltext" | "fingerprint", unknown>>;
  remediation?: string;
  resolved_minutes?: number;
}
export interface NarrationData {
  topic?: "precedent" | "general" | "recommendation" | "memory";
  text?: string;
  recommendations?: RecommendationRef[];
  matches?: PrecedentMatch[];
  precedents?: PrecedentRecord[];
  degraded_reason?: DegradedReason | string;
}

export interface ErrorData {
  code: "rate_limited" | "session_expired" | "action_failed" | "failed";
  message: string;
  retry_after_s?: number;
  action?: string;
}

export interface DoneData {
  ms: number;
}

export type VerifyState = "VERIFIED" | "DRIFTED" | "UNANCHORED" | "FALSIFIED" | "TAMPERED";
