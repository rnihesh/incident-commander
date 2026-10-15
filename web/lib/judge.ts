// Judge mode (U14): typed calls for /api/judge/*, the tamper payload builder, the
// in-browser commitment check and the reveal comparison view. The answer key never
// reaches the browser before the reveal; the page only ever holds the commitment.
import { API_URL, DEFAULT_TIMEOUT_MS, LONG_TIMEOUT_MS, fetchWithTimeout, type FetchLike, type VerifyResponse } from "./api";
import type { Tone } from "@/components/ui";

export type JudgeFaultClass = "bad_deploy" | "config_change" | "flag_flip" | "expiry" | "undeclared_coupling";
export const JUDGE_FAULT_CLASSES: { id: JudgeFaultClass; label: string; heldOut: boolean }[] = [
  { id: "bad_deploy", label: "bad deploy", heldOut: false },
  { id: "config_change", label: "config change", heldOut: false },
  { id: "flag_flip", label: "flag flip", heldOut: false },
  { id: "expiry", label: "expiry (held out)", heldOut: true },
  { id: "undeclared_coupling", label: "undeclared coupling (held out)", heldOut: true },
];

export interface JudgeTarget {
  id: string;
  name: string;
  region: string;
  tier: number;
  domain: string;
  x: number;
  y: number;
  upstream_services: number;
  has_upstream_path: boolean;
  reason: string | null;
  unavailable: Partial<Record<JudgeFaultClass, string>>;
}
export interface TargetsResponse {
  t_ref: number;
  total: number;
  fault_classes: JudgeFaultClass[];
  services: JudgeTarget[];
  edges: { source: string; target: string }[];
}
export interface ServiceChange {
  id: string;
  kind: string;
  summary: string;
  valid_from: number;
  recorded_at: number | null;
}
export interface ServiceChanges {
  service_id: string;
  changes: ServiceChange[];
  expiries: { id: string; kind: string; summary: string; valid_to: number }[];
}
export interface JudgeInjectResponse {
  run_id: string;
  incident_id: string;
  alert_service_id: string;
  alert_time: number;
  commitment: string;
  reveal_after_s: number;
  events_url: string;
  degraded: boolean;
}
export type ComparisonResult = "match" | "partial" | "miss" | "no_claim";
export interface Comparison {
  run_id: string | null;
  finished: boolean;
  outcome: string | null;
  suspect_id: string | null;
  rival_ids: string[];
  claimed_class: string | null;
  headline: string | null;
  result: ComparisonResult;
  explanation: string;
}
export interface RevealResponse {
  incident_id: string;
  published_commitment: string;
  revealed: { fault_id: string; fault_class: string; root_id: string; nonce: string };
  preimage: string;
  commitment: string | null;
  recomputed: string;
  commitment_ok: boolean;
  timed_out: boolean;
  elapsed_s: number;
  comparison: Comparison;
  rederived: Comparison | null;
}

export type TamperAction =
  | { action: "add_deploy_upstream"; service_id: string; minutes_before_onset: number; as_if_known_at_alert: boolean; summary?: string }
  | { action: "remove_edge"; from_service_id: string; to_service_id: string }
  | { action: "change_timestamp"; change_id: string; new_valid_from: number };
export type TamperKind = TamperAction["action"];

export interface TamperResponse {
  action: TamperKind;
  incident_id: string;
  graph: string;
  t: number;
  earliest_onset: number | null;
  summary: string;
  node: string;
  statements: { cypher: string; params: Record<string, unknown> }[];
  created?: { id: string; service_id: string; valid_from: number; recorded_at: number; late_arriving: boolean; summary: string };
  removed?: { type: string; properties: Record<string, unknown> }[];
  changed?: { id: string; service_id: string; valid_from_before: number; valid_from_after: number };
}
export interface RederiveResponse {
  run_id: string;
  incident_id: string;
  alert_service_id: string;
  alert_time: number;
  previous_run_id: string | null;
  events_url: string;
}

/** A judge route refused (bad target, too early to reveal, unknown id, limits). */
export class JudgeError extends Error {
  readonly status: number;
  readonly retryAfter?: number;
  readonly fallback: boolean;
  constructor(status: number, message: string, retryAfter?: number, fallback = false) {
    super(message);
    this.name = "JudgeError";
    this.status = status;
    this.retryAfter = retryAfter;
    this.fallback = fallback;
  }
}

async function judgeRequest<T>(path: string, init: RequestInit = {}, f?: FetchLike, timeoutMs = DEFAULT_TIMEOUT_MS): Promise<T> {
  const doFetch: FetchLike = f ?? ((i, o) => fetch(i, o));
  let res: Response;
  try {
    res = await fetchWithTimeout(doFetch, `${API_URL}${path}`, {
      ...init,
      credentials: "include",
      headers: { ...(init.body ? { "Content-Type": "application/json" } : {}), ...init.headers },
    }, timeoutMs);
  } catch (e) {
    throw new JudgeError(0, e instanceof Error ? e.message : "network error");
  }
  if (res.ok) return (await res.json()) as T;
  let body: Record<string, unknown> = {};
  try {
    body = ((await res.json()) as Record<string, unknown>) ?? {};
  } catch {
    /* non-JSON error body */
  }
  const msg =
    (typeof body.reason === "string" && body.reason) ||
    (typeof body.error === "string" && body.error) ||
    (typeof body.detail === "string" && body.detail) ||
    `${init.method ?? "GET"} ${path} failed with ${res.status}`;
  const retry = typeof body.retry_after === "number" ? body.retry_after : undefined;
  throw new JudgeError(res.status, msg, retry, body.fallback === "recorded_run");
}

const post = (body: unknown = {}): RequestInit => ({ method: "POST", body: JSON.stringify(body) });
const iidPath = (iid: string) => `/api/judge/${encodeURIComponent(iid)}`;

export const getTargets = (q?: string, f?: FetchLike) =>
  judgeRequest<TargetsResponse>("/api/judge/targets", post(q ? { q } : {}), f);

export const getServiceChanges = (serviceId: string, f?: FetchLike) =>
  judgeRequest<ServiceChanges>(`/api/judge/services/${encodeURIComponent(serviceId)}/changes`, {}, f);

export const judgeInject = (target_service_id: string, fault_class: JudgeFaultClass, f?: FetchLike) =>
  judgeRequest<JudgeInjectResponse>("/api/judge/inject", post({ target_service_id, fault_class }), f);

export const revealFault = (incidentId: string, f?: FetchLike) =>
  judgeRequest<RevealResponse>(`${iidPath(incidentId)}/reveal`, post(), f);

export const tamper = (incidentId: string, action: TamperAction, f?: FetchLike) =>
  judgeRequest<TamperResponse>(`${iidPath(incidentId)}/tamper`, post(action), f);

export const rederive = (incidentId: string, f?: FetchLike) =>
  judgeRequest<RederiveResponse>(`${iidPath(incidentId)}/rederive`, post(), f);

export const fragilityMap = (incidentId: string, f?: FetchLike) =>
  judgeRequest<FragilityResponse>(`${iidPath(incidentId)}/fragility`, post(), f, LONG_TIMEOUT_MS);

// --- fragility map (U9) -------------------------------------------------------------------

/** One single edit tried on a throwaway copy of the snapshot, and what verify_claim said. */
export interface FragilityEdit {
  kind: TamperKind;
  label: string;
  /** The exact tamper body that applies this edit to the world; null when no form reaches it. */
  tamper: TamperAction | null;
  prediction: { exact: boolean; note: string; sources?: string[] };
  verdict: "VERIFIED" | "REFUTED" | "RIVAL";
  clause: string | null;
  detail: string;
  rivals: string[];
  flips: boolean;
  copy_ms: number;
  hop?: { from: string; to: string };
  near?: { id: string; service_id: string; clause: string | null };
}
export interface FragilityResponse {
  incident_id: string;
  root_id: string;
  applicable: boolean;
  baseline: { verdict: string; clause: string | null; detail: string; rivals: string[] };
  earliest_onset: number | null;
  path: string[];
  tried: FragilityEdit[];
  flips: FragilityEdit[];
  copies: number;
  skipped: number;
  /** True when the server's time budget ran out before every planned edit was tried. */
  out_of_time?: boolean;
  max_edits: number;
  copy_ms: number | null;
  detail: string;
}

/** The flipping edits the tamper form can apply, in the order they were tried. */
export function trySuggestions(resp: FragilityResponse | null): (FragilityEdit & { tamper: TamperAction })[] {
  return (resp?.flips ?? []).filter((e): e is FragilityEdit & { tamper: TamperAction } => e.tamper !== null);
}

/** A fragility map computed at world-epoch `computedAt` is stale once a tamper (or a re-derive)
 * has bumped the panel's epoch past it. */
export const fragilityStale = (computedAt: number, currentEpoch: number): boolean => computedAt !== currentEpoch;

/** Tamper body -> the tab and form fields that rebuild exactly that body. `onset` is the
 * panel's earliest onset (the timestamp field is relative to it). */
export function tamperFormFor(a: TamperAction, onset: number | null): { kind: TamperKind; form: TamperForm } {
  if (a.action === "add_deploy_upstream") {
    return {
      kind: a.action,
      form: { ...EMPTY_TAMPER_FORM, service_id: a.service_id, minutes_before_onset: String(a.minutes_before_onset), as_if_known_at_alert: a.as_if_known_at_alert },
    };
  }
  if (a.action === "remove_edge") {
    return { kind: a.action, form: { ...EMPTY_TAMPER_FORM, from_service_id: a.from_service_id, to_service_id: a.to_service_id } };
  }
  const shift = onset === null ? "" : String(Math.round((a.new_valid_from - onset) / 60));
  return { kind: a.action, form: { ...EMPTY_TAMPER_FORM, change_id: a.change_id, shift_minutes: shift } };
}

/** What a flipping edit does to the verdict, in a few words. */
export function flipText(e: FragilityEdit): string {
  if (e.verdict === "RIVAL") return `RIVAL: ${e.rivals.join(", ") || "another candidate"} qualifies too`;
  return `REFUTED on ${e.clause ?? "a clause"}`;
}

// --- tamper forms -------------------------------------------------------------------------

export interface TamperForm {
  service_id: string;
  minutes_before_onset: string;
  as_if_known_at_alert: boolean;
  from_service_id: string;
  to_service_id: string;
  change_id: string;
  /** Minutes relative to the earliest onset (negative = before it). */
  shift_minutes: string;
}
export const EMPTY_TAMPER_FORM: TamperForm = {
  service_id: "",
  minutes_before_onset: "5",
  as_if_known_at_alert: true,
  from_service_id: "",
  to_service_id: "",
  change_id: "",
  shift_minutes: "1",
};

/** Form -> request body, or an error message. `onset` is the incident's earliest onset (epoch s). */
export function buildTamperPayload(kind: TamperKind, form: TamperForm, onset: number | null): TamperAction | { error: string } {
  const int = (s: string) => (/^-?\d+$/.test(s.trim()) ? Number(s.trim()) : NaN);
  if (kind === "add_deploy_upstream") {
    if (!form.service_id) return { error: "Choose the service that gets the deploy." };
    const m = int(form.minutes_before_onset);
    if (!Number.isFinite(m) || m < -720 || m > 1440) return { error: "Minutes before onset must be a whole number from -720 to 1440." };
    return { action: kind, service_id: form.service_id, minutes_before_onset: m, as_if_known_at_alert: form.as_if_known_at_alert };
  }
  if (kind === "remove_edge") {
    if (!form.from_service_id || !form.to_service_id) return { error: "Choose both ends of the edge." };
    if (form.from_service_id === form.to_service_id) return { error: "An edge needs two different services." };
    return { action: kind, from_service_id: form.from_service_id, to_service_id: form.to_service_id };
  }
  if (!form.change_id) return { error: "Choose the change to move." };
  const shift = int(form.shift_minutes);
  if (!Number.isFinite(shift) || onset === null) return { error: "Shift must be a whole number of minutes." };
  return { action: kind, change_id: form.change_id, new_valid_from: onset + shift * 60 };
}

/** One line for the confirmation step, before anything is written. */
export function describeTamper(a: TamperAction): string {
  if (a.action === "add_deploy_upstream") {
    const when = a.minutes_before_onset >= 0 ? `${a.minutes_before_onset} min before` : `${-a.minutes_before_onset} min after`;
    const rec = a.as_if_known_at_alert ? "recorded before the alert (as-known sees it)" : "recorded after the alert (late-arriving)";
    return `Create a deploy on ${a.service_id}, effective ${when} the earliest onset, ${rec}.`;
  }
  if (a.action === "remove_edge") return `Delete every DEPENDS_ON / OBSERVED_CALL edge from ${a.from_service_id} to ${a.to_service_id}.`;
  return `Move ${a.change_id} so it takes effect at ${new Date(a.new_valid_from * 1000).toISOString()}.`;
}

// --- commit-reveal ------------------------------------------------------------------------

/** sha256(preimage + nonce) as lowercase hex, computed in the browser (Web Crypto). */
export async function recomputeCommitment(preimage: string, nonce: string): Promise<string> {
  const data = new TextEncoder().encode(preimage + nonce);
  const digest = await crypto.subtle.digest("SHA-256", data);
  return [...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

export const RESULT_VIEW: Record<ComparisonResult, { label: string; tone: Tone; detail: string }> = {
  match: { label: "MATCH", tone: "verified", detail: "The agent named the committed root entity." },
  partial: { label: "PARTIAL", tone: "drifted", detail: "Right fault class, different root entity." },
  miss: { label: "MISS", tone: "falsified", detail: "The agent named a different entity of a different class." },
  no_claim: { label: "NO CLAIM", tone: "muted", detail: "INCONCLUSIVE, FAILED or no verdict: the agent made no claim." },
};

/** Whether the hash shown before the run equals the one recomputed from the revealed values. */
export function hashCheck(published: string, reveal: RevealResponse, browserHash: string | null) {
  const server = reveal.recomputed === published;
  const browser = browserHash === null ? null : browserHash === published;
  return { server, browser, ok: server && browser !== false };
}

// --- verify -------------------------------------------------------------------------------

export interface FlippedProof {
  seq: number;
  kind: string;
  entity_id: string;
  responsible: string[];
  late: boolean;
}
/** The proofs a Verify after tampering reported FALSIFIED, with their responsible nodes. */
export function flippedProofs(resp: VerifyResponse | null): FlippedProof[] {
  if (!resp) return [];
  return resp.proofs
    .filter((p) => p.status === "FALSIFIED")
    .map((p) => ({ seq: p.seq, kind: p.kind, entity_id: p.entity_id, responsible: p.responsible ?? [], late: !!p.late_arriving }));
}
