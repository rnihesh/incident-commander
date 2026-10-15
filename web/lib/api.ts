// Typed fetch helpers for the Incident Commander API. Every call carries the
// session cookie (`ic_sid`) and maps refusals to typed errors so callers can
// fall back to the recorded run.
import type { RunEvent, ToolResult } from "./events";
import type { ReplayResponse } from "./replay";
import type { ChallengeBudget, ChallengeResponse } from "./challenge";

export const API_URL = (process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000").replace(/\/+$/, "");

export type FaultClass = "bad_deploy" | "config_change" | "flag_flip";
export const FAULT_CLASSES: FaultClass[] = ["bad_deploy", "config_change", "flag_flip"];

export type FetchLike = (input: string, init?: RequestInit) => Promise<Response>;

/** Base class: the backend refused or is unavailable; play the recorded run. */
export class FallbackError extends Error {
  readonly status: number;
  readonly reason?: string;
  constructor(status: number, reason?: string) {
    super(reason ? `Live backend unavailable (${reason})` : "Live backend unavailable");
    this.name = "FallbackError";
    this.status = status;
    this.reason = reason;
  }
}
/** 429: per-session or global investigation cap. */
export class RateLimitedError extends FallbackError {
  constructor(reason?: string) {
    super(429, reason);
    this.name = "RateLimitedError";
    this.message = reason ? `Rate limited (${reason})` : "Rate limited";
  }
}
/** Transport failure or an unexpected non-2xx response. */
export class NetworkError extends Error {
  readonly status?: number;
  constructor(message: string, status?: number) {
    super(message);
    this.name = "NetworkError";
    this.status = status;
  }
}

/** 409 `{retry: true}`: the incident's snapshots are still building; try again shortly. */
export class RetryableError extends Error {
  readonly status: number;
  constructor(message: string, status = 409) {
    super(message);
    this.name = "RetryableError";
    this.status = status;
  }
}

export interface InjectResponse {
  run_id: string;
  incident_id: string;
  alert_service_id: string;
  /** Epoch seconds. */
  alert_time: number;
  degraded: boolean;
}

export interface VerifyProof {
  seq: number;
  kind: string;
  entity_id: string;
  status: string;
  responsible: string[];
  late_arriving: boolean;
  tampered_reason?: string;
  claim?: string;
}
export interface VerifyResponse {
  overall: "VERIFIED" | "DRIFTED" | "UNANCHORED" | "FALSIFIED" | "TAMPERED";
  chain_ok: boolean;
  anchor_ok: boolean;
  proofs: VerifyProof[];
  tampered_reasons: string[];
}

async function readJson(res: Response): Promise<Record<string, unknown> | null> {
  try {
    const v = await res.json();
    return v && typeof v === "object" ? (v as Record<string, unknown>) : null;
  } catch {
    return null;
  }
}

/** Error text from `{reason}` / `{error}` / `{detail}` (string or FastAPI validation array). */
function errorText(body: Record<string, unknown> | null): string | undefined {
  if (!body) return undefined;
  for (const k of ["reason", "error"]) if (typeof body[k] === "string" && body[k]) return body[k] as string;
  const d = body.detail;
  if (typeof d === "string" && d) return d;
  if (Array.isArray(d) && d.length) {
    return d
      .map((x) => {
        if (typeof x === "string") return x;
        const o = (x ?? {}) as { loc?: unknown; msg?: unknown };
        const loc = Array.isArray(o.loc) ? o.loc.join(".") : "";
        const m = typeof o.msg === "string" ? o.msg : "invalid";
        return loc ? `${loc}: ${m}` : m;
      })
      .join("; ");
  }
  return undefined;
}

/** Client-side give-up times: a hung backend must surface as an error, not a spinner forever. */
export const DEFAULT_TIMEOUT_MS = 30_000;
/** Fragility maps and challenges rebuild a snapshot, so they get longer. */
export const LONG_TIMEOUT_MS = 60_000;

/**
 * `doFetch` with an abort after `timeoutMs`, unless the caller passed its own signal. The timeout
 * also rejects the returned promise, so a fetch that ignores the signal still ends.
 */
export async function fetchWithTimeout(doFetch: FetchLike, url: string, init: RequestInit, timeoutMs: number): Promise<Response> {
  if (init.signal) return doFetch(url, init);
  const ctrl = new AbortController();
  let timer: ReturnType<typeof setTimeout> | undefined;
  const timedOut = new Promise<never>((_, reject) => {
    timer = setTimeout(() => {
      ctrl.abort();
      reject(new Error(`request timed out after ${Math.round(timeoutMs / 1000)} s`));
    }, timeoutMs);
  });
  try {
    return await Promise.race([doFetch(url, { ...init, signal: ctrl.signal }), timedOut]);
  } finally {
    clearTimeout(timer);
  }
}

async function request<T>(path: string, init: RequestInit = {}, f?: FetchLike, timeoutMs = DEFAULT_TIMEOUT_MS): Promise<T> {
  const doFetch: FetchLike = f ?? ((i, o) => fetch(i, o));
  let res: Response;
  try {
    res = await fetchWithTimeout(doFetch, `${API_URL}${path}`, {
      ...init,
      credentials: "include",
      headers: { ...(init.body ? { "Content-Type": "application/json" } : {}), ...init.headers },
    }, timeoutMs);
  } catch (e) {
    throw new NetworkError(e instanceof Error ? e.message : "network error");
  }
  if (res.ok) return (await res.json()) as T;
  const body = await readJson(res);
  const reason = typeof body?.reason === "string" ? body.reason : undefined;
  const text = errorText(body);
  if (res.status === 409 && body?.retry === true) throw new RetryableError(text ?? "still building, retry shortly", 409);
  if (res.status === 429) throw new RateLimitedError(reason);
  if (res.status === 503 || body?.fallback === "recorded_run") throw new FallbackError(res.status, reason);
  throw new NetworkError(text ?? `${init.method ?? "GET"} ${path} failed with ${res.status}`, res.status);
}

export interface RetryOpts {
  /** Total tries (default 5). */
  attempts?: number;
  /** Injectable delay, for tests. */
  sleep?: (ms: number) => Promise<void>;
}

/** Retry a call while the backend answers 409 `{retry: true}`: 400 ms doubling to at most 2 s. */
export async function withRetry<T>(fn: () => Promise<T>, opts: RetryOpts = {}): Promise<T> {
  const attempts = opts.attempts ?? 5;
  const sleep = opts.sleep ?? ((ms: number) => new Promise<void>((r) => setTimeout(r, ms)));
  for (let i = 1; ; i++) {
    try {
      return await fn();
    } catch (e) {
      if (!(e instanceof RetryableError) || i >= attempts) throw e;
      await sleep(Math.min(400 * 2 ** (i - 1), 2000));
    }
  }
}

// The API requires a JSON content type on every state-changing route, so always send a body.
const post = (body: unknown = {}): RequestInit => ({ method: "POST", body: JSON.stringify(body) });

export const createSession = (f?: FetchLike) => request<{ created: boolean; idle_ttl_s: number }>("/api/session", post(), f);

/** Incident ids the caller's session has started (empty without a session). Never creates a session. */
export const getSessionIncidents = async (f?: FetchLike): Promise<string[]> => {
  const body = await request<{ incidents?: string[] }>("/api/session/incidents", {}, f);
  return body.incidents ?? [];
};

export type StormFault = FaultClass | "joint";
export interface StormResponse {
  run_id: string;
  storm_id: string;
  pages: number;
  started_at: number;
  ended_at: number;
  events_url: string;
}

/** Fire an alert storm (U17): 2-3 independent faults, hundreds of pages, one collapse run. */
export const startStorm = (opts: { seed?: number; faults?: StormFault[] } = {}, f?: FetchLike) =>
  request<StormResponse>("/api/storm", post(opts), f);

export const injectFault = (fault_class: FaultClass, seed?: number, f?: FetchLike) =>
  request<InjectResponse>("/api/inject", post(seed === undefined ? { fault_class } : { fault_class, seed }), f);

// The backend's VerifyReport names the overall result `status`; the UI reads `overall`.
export const verifyIncident = async (incidentId: string, f?: FetchLike): Promise<VerifyResponse> => {
  const r = await request<VerifyResponse & { status?: VerifyResponse["overall"] }>(
    `/api/incidents/${encodeURIComponent(incidentId)}/verify`,
    post(),
    f,
  );
  return { ...r, overall: r.overall ?? (r.status as VerifyResponse["overall"]) };
};

export const exonerate = (incidentId: string, change_id: string, f?: FetchLike, retry?: RetryOpts) =>
  withRetry(
    () => request<ToolResult>(`/api/incidents/${encodeURIComponent(incidentId)}/exonerate`, post({ change_id }), f),
    retry,
  );

/** One operator of a FalkorDB execution plan; `records`/`ms` are null in an EXPLAIN plan. */
export interface XrayOp {
  name: string;
  args: string | null;
  records: number | null;
  ms: number | null;
  children: XrayOp[];
}
/** Which step to X-ray, by id only: the server re-derives the query (it never takes Cypher). */
export type XrayStep = { funnel: string } | { proof: number };
export interface XrayResponse {
  incident_id: string;
  step: XrayStep;
  graph: string;
  cypher: string;
  params: Record<string, unknown>;
  explain: XrayOp;
  profile: XrayOp;
  /** The plan of the rejected design (valid-time filter inside the traversal); absent on older servers. */
  naive?: { cypher: string; graph_label: string; explain: XrayOp };
}

/** GRAPH.EXPLAIN plus a live GRAPH.PROFILE of a funnel cut's or proof's query (console only). */
export const xrayStep = (incidentId: string, step: XrayStep, f?: FetchLike, retry?: RetryOpts) =>
  withRetry(
    () => request<XrayResponse>(`/api/incidents/${encodeURIComponent(incidentId)}/xray`, post(step), f),
    retry,
  );

/** Judge challenge (R5): verify_claim on a fresh as-of build of the session world. Two per
 * incident; the third is a 429 (RateLimitedError). Never changes the incident. */
export const challengeIncident = (incidentId: string, change_id: string, f?: FetchLike, retry?: RetryOpts) =>
  withRetry(
    () => request<ChallengeResponse>(`/api/incidents/${encodeURIComponent(incidentId)}/challenge`, post({ change_id }), f, LONG_TIMEOUT_MS),
    retry,
  );

export const getChallengeBudget = (incidentId: string, f?: FetchLike) =>
  request<ChallengeBudget>(`/api/incidents/${encodeURIComponent(incidentId)}/challenge`, {}, f);

/** Ask the graph: one plain-English question on the session's world (starts a session if
 *  needed). `incidentId` adds that incident as context. 429 past the hourly question cap. */
export const askGraph = (question: string, incidentId?: string | null, f?: FetchLike) =>
  request<unknown>("/api/ask", post(incidentId ? { question, incident_id: incidentId } : { question }), f, LONG_TIMEOUT_MS);

export interface IncidentGraphNode {
  id: string;
  name: string;
  region?: string;
  tier?: number;
  x: number;
  y: number;
  symptomatic: boolean;
  /** Epoch seconds. */
  onset: number | null;
  signature: string | null;
  alerting: boolean;
}
export interface IncidentGraphResponse {
  incident_id: string;
  /** Epoch seconds. */
  t: number;
  alert_service_id: string;
  nodes: IncidentGraphNode[];
  edges: { source: string; target: string }[];
  changes: { id: string; service_id: string; kind: string; valid_from: number; recorded_at: number | null }[];
}

export const getIncidentGraph = (incidentId: string, f?: FetchLike) =>
  request<IncidentGraphResponse>(`/api/incidents/${encodeURIComponent(incidentId)}/graph`, {}, f);

export const getIncidentReplay = (incidentId: string, view: "true" | "known", f?: FetchLike, retry?: RetryOpts) =>
  withRetry(
    () => request<ReplayResponse>(`/api/incidents/${encodeURIComponent(incidentId)}/replay?view=${view}`, {}, f),
    retry,
  );

// The API wraps the saved stream as {recorded: true, events}; accept a bare list too.
export const getRecordedHero = async (f?: FetchLike): Promise<RunEvent[]> => {
  const body = await request<RunEvent[] | { events?: RunEvent[] }>("/api/recorded/hero", {}, f);
  return Array.isArray(body) ? body : (body.events ?? []);
};

/** The incident graph saved with the recorded run (same shape as the live incident graph). */
export const getRecordedHeroGraph = (f?: FetchLike) => request<IncidentGraphResponse>("/api/recorded/hero/graph", {}, f);

/** One incident of the published learning-curve run (``ic learn``). */
export interface LearningCurveIncident {
  incident_id: string;
  alert_service: string;
  verdict: string | null;
  correct: boolean;
  tool_calls: number | null;
  steps: number | null;
  ms: number;
  recall_used: boolean;
  memory_seeded: boolean;
  /** Forgetting curve (newer runs only): the learned edge's confidence after this incident
   *  (also when the incident left it untouched) and before it. */
  edge_confidence?: number | null;
  edge_confidence_before?: number | null;
  /** This incident's write-back halved the learned edge (both ends hurt, no path through it). */
  contradicted?: boolean;
  /** Recall seeded a suspect because of the learned edge. */
  edge_seeded?: boolean;
  phase?: "learn" | "forget";
  /** The forget-phase incident run after confidence fell under the recall threshold. */
  check?: boolean;
}
export interface LearningCurveForget {
  threshold: number;
  contradictions: number;
  check_incident?: string | null;
  change?: { id: string; summary: string; via?: string };
}
export interface LearningCurveResponse {
  meta: { runs: number; coupling?: { caller: string; callee: string }; llm?: string; seed?: number; forget?: LearningCurveForget | null };
  incidents: LearningCurveIncident[];
  summary?: { first_tool_calls?: number; second_tool_calls?: number | null; later_mean_tool_calls?: number | null; reduction_pct?: number | null };
}

export const getLearningCurve = (f?: FetchLike) => request<LearningCurveResponse>("/api/learning-curve", {}, f);

/** Published benchmark report (`ic bench`, GET /api/benchmark): public and read-only. */
export interface BenchLatencyStat {
  p50_ms: number;
  p95_ms: number;
  n: number;
}
export interface BenchLatencyOp {
  label: string;
  falkordb: BenchLatencyStat;
  postgres: BenchLatencyStat;
  checked: number;
  agreed: number;
  exact?: { falkordb: number; postgres: number };
  /** Scale section only: mean services reached (closure) or mean hops (path). */
  mean_size?: number | null;
}
export interface BenchLatency {
  world: string;
  iterations: number;
  counts: Record<string, number>;
  ops: Record<string, BenchLatencyOp>;
  error?: string;
}
export interface BenchScale extends BenchLatency {
  n_services_requested: number;
  seed: number;
  closure_depth: number;
  timings_s?: Record<string, number>;
  notes?: string[];
  generated_at?: string;
  commit?: string;
}
export interface BenchApproachSummary {
  label: string;
  n: number;
  n_seen: number;
  n_heldout: number;
  correct: number;
  accuracy_seen: number | null;
  accuracy_heldout: number | null;
  accuracy_overall: number | null;
  mean_ms: number | null;
  mean_llm_tokens: number;
  mean_prompt_tokens_est: number;
  mean_tool_calls: number;
  errors?: number;
  truncated?: number;
}
export interface BenchLiveRun {
  model: string;
  label?: string;
  cases: number;
  n_seen: number;
  n_heldout: number;
  commit?: string | null;
  generated_at?: string | null;
  approaches?: string[];
  summary: Record<string, Partial<BenchApproachSummary> & { correct: number; n: number; fallback?: number }>;
  per_class?: Record<string, Record<string, { n: number; correct: number }>>;
  /** Where the run's own report lives (relative to the bench results directory). */
  source?: string;
  /** `ic bench --why-wrong`: every wrong prediction re-checked with verify_claim. */
  why_wrong?: BenchWhyRow[] | null;
}
/** One wrong live-LLM prediction. verdict is REFUTED / RIVAL / VERIFIED from verify_claim, or
 * no_claim (no id named), cannot_regenerate, unknown_id; clause is set for REFUTED. */
export interface BenchWhyRow {
  case: string;
  fault_class: string;
  held_out: boolean;
  approach: string;
  predicted: string | null;
  verdict: string;
  clause: string | null;
  detail: string | null;
  query_text: string | null;
}
export interface BenchmarkResponse {
  meta: {
    seed: number;
    cases: number;
    n_seen: number;
    n_heldout: number;
    commit: string;
    dirty?: boolean;
    classes: string[];
    heldout_classes: string[];
    case_seeds?: number[];
    case_seeds_digest?: string;
    approaches: { name: string; label: string }[];
    llm: string;
    world?: string;
    notes?: string[];
    generated_at?: string;
  };
  summary: Record<string, BenchApproachSummary>;
  per_class: Record<string, Record<string, { n: number; correct: number }>>;
  latency?: BenchLatency | null;
  scale?: BenchScale | null;
  learning_curve?: LearningCurveResponse | null;
  live_llm_runs?: BenchLiveRun[] | null;
}

export const getBenchmark = (f?: FetchLike) => request<BenchmarkResponse>("/api/benchmark", {}, f);

/** Public proof page (GET /api/proof/{ref}): read from the PR's copies; no session. */
export interface ProofPageProof {
  seq: number;
  proof_id: string;
  hypothesis_id: string;
  kind: string;
  entity_id: string;
  claim: string;
  state: "HOLDS" | "REFUTED" | string;
  holds: boolean;
  conclusion: Record<string, unknown>;
  result_hash: string;
  hash: string;
  prev_hash: string | null;
  cypher: string[] | string;
  params: Record<string, unknown>[] | Record<string, unknown>;
  executed_at?: number;
}
export interface ProofVerifyProof {
  seq: number;
  proof_id: string;
  kind: string;
  entity_id: string;
  claim: string;
  recorded_state: string;
  status: VerifyResponse["overall"];
  responsible: string[];
  late_arriving: boolean;
  late_ids: string[];
  hash_matches: boolean;
  error: string | null;
  tampered_reason: string | null;
}
export interface ProofVerifyResponse {
  incident_id: string;
  status: VerifyResponse["overall"];
  chain_ok: boolean;
  broken_at: number | null;
  anchor_ok: boolean | null;
  stored_head: string | null;
  recomputed_head: string | null;
  anchored_head: string | null;
  proofs: ProofVerifyProof[];
  tampered_reasons: string[];
  ms: number;
  verified_at: string | null;
  cached?: boolean;
  commit_status?: { posted: boolean; state: string; simulated?: boolean; context?: string; reason?: string; error?: string };
}
export interface ProofPageResponse {
  ref: string;
  incident_id: string;
  title: string;
  alert_service: string | null;
  alert_time: string | null;
  verdict: string;
  suspect: string | null;
  chain_head: string | null;
  anchored: boolean;
  anchor_matches: boolean;
  proofs: ProofPageProof[];
  chain_error: string | null;
  rehearsal: { result: string; change_id: string; checked: number | null; detail: string | null; clone_ms: number | null } | null;
  pr: { url: string | null; number: number | null; simulated: boolean | null; opened_at: string | null; expires_at: string | null };
  status_context: string;
  last_verify: ProofVerifyResponse | null;
}

const PROOF_REF = /^[a-f0-9]{24}$/;
export const isProofRef = (ref: string) => PROOF_REF.test(ref);

export const getProof = (ref: string, f?: FetchLike) =>
  request<ProofPageResponse>(`/api/proof/${encodeURIComponent(ref)}`, {}, f);

/** Re-run Verify on the PR's copies (rate-limited per IP, debounced per proof). */
export const reverifyProof = (ref: string, f?: FetchLike) =>
  request<ProofVerifyResponse>(`/api/proof/${encodeURIComponent(ref)}/verify`, post(), f);
