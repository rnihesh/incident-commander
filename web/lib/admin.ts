// Admin usage panel (/admin): typed reads of /api/admin/* with a bearer token. No cookies are
// sent; the token lives in sessionStorage only.
import { API_URL, DEFAULT_TIMEOUT_MS, fetchWithTimeout, type FetchLike } from "./api";

export const ADMIN_TOKEN_KEY = "ic_admin_token";
export const ADMIN_REFRESH_MS = 30_000;

export interface AdminModelSpend {
  model: string;
  calls: number;
  prompt_tokens: number;
  completion_tokens: number;
  usd: number;
}
export interface AdminSpend {
  usd: number;
  calls: number;
  prompt_tokens: number;
  completion_tokens: number;
  by_model: AdminModelSpend[];
}
export interface AdminRunStats {
  total: number;
  degraded: number;
  prs: number;
  errors: number;
  usd: number;
  tokens: number;
  avg_duration_ms: number | null;
  by_verdict: Record<string, number>;
  by_fault_class: Record<string, number>;
  by_kind: Record<string, number>;
}
export interface AdminDay {
  day: string;
  usd: number;
  tokens: number;
  runs: number;
  degraded_runs: number;
  sessions: number;
  asks: number;
  clients: number;
  refusals: number;
}
export interface AdminFalkor {
  reachable: boolean;
  error?: string;
  used_memory?: number;
  used_memory_human?: string;
  peak_memory_human?: string;
  maxmemory?: number;
  maxmemory_human?: string;
  maxmemory_policy?: string;
  used_pct?: number | null;
  graphs?: { total: number; session: number; pr: number; bench: number; other: number };
}
export interface AdminSummary {
  spend: { today: AdminSpend; last_7_days: AdminSpend; all_time: AdminSpend };
  runs: { today: AdminRunStats; last_7_days: AdminRunStats };
  daily: AdminDay[];
  sessions: { active: number; max: number; seen_last_5_min: number; created_today: number; runs_in_flight: number };
  clients: { unique_today: number; top_today: { client: string; events: number }[] };
  activity_today: Record<string, number>;
  refusals_today: { total: number; by_kind: Record<string, number> };
  admin_auth_failures_today: number;
  breaker: { daily_usd: number; spent_today: number; remaining_usd: number; tripped: boolean };
  falkordb: AdminFalkor;
  process: { started_at: string; uptime_s: number };
  config: { llm: { provider: string; model: string | null; configured: boolean }; limits: Record<string, number> };
  generated_at: string;
  day_basis: string;
}
export interface AdminRun {
  run_id: string;
  time: string;
  kind: string;
  incident_id: string;
  fault_class: string | null;
  verdict: string | null;
  degraded: boolean | null;
  degraded_reason: string | null;
  final_state: string | null;
  tool_calls: number;
  prompt_tokens: number | null;
  completion_tokens: number | null;
  tokens: number | null;
  usd: number | null;
  duration_ms: number | null;
  pr: boolean | null;
  simulated: boolean | null;
  error: string | null;
}

/** A refused admin read: `status` 404 (panel off), 401 (wrong token), 429 (locked out). */
export class AdminError extends Error {
  readonly status: number;
  constructor(status: number, message: string) {
    super(message);
    this.name = "AdminError";
    this.status = status;
  }
}

export function adminErrorText(status: number, retryAfter: string | null): string {
  if (status === 404) return "The admin panel is off on this server (IC_ADMIN_TOKEN is not set).";
  if (status === 401) return "That token was not accepted.";
  if (status === 429) return `Too many failed attempts from this address; try again in ${retryAfter ?? "a few"} s.`;
  return `The admin API answered ${status}.`;
}

export async function getAdmin<T>(path: string, token: string, f?: FetchLike): Promise<T> {
  const doFetch: FetchLike = f ?? ((i, o) => fetch(i, o));
  const res = await fetchWithTimeout(
    doFetch,
    `${API_URL}/api/admin${path}`,
    { credentials: "omit", cache: "no-store", headers: { Authorization: `Bearer ${token}` } },
    DEFAULT_TIMEOUT_MS,
  );
  if (!res.ok) throw new AdminError(res.status, adminErrorText(res.status, res.headers.get("retry-after")));
  return (await res.json()) as T;
}

export const getAdminSummary = (token: string, f?: FetchLike) => getAdmin<AdminSummary>("/summary", token, f);
export const getAdminRuns = (token: string, limit = 50, f?: FetchLike) =>
  getAdmin<{ runs: AdminRun[] }>(`/runs?limit=${limit}`, token, f).then((b) => b.runs);

// --- formatting ----------------------------------------------------------------------------

export function fmtUsd(x: number | null | undefined): string {
  if (x == null) return "n/a";
  if (x === 0) return "$0";
  if (x < 0.01) return `$${x.toFixed(4)}`;
  return `$${x.toFixed(2)}`;
}

export function fmtInt(x: number | null | undefined): string {
  return x == null ? "n/a" : Math.round(x).toLocaleString("en-US");
}

export function fmtTokens(x: number | null | undefined): string {
  if (x == null) return "n/a";
  if (x >= 1_000_000) return `${(x / 1_000_000).toFixed(1)}M`;
  if (x >= 10_000) return `${Math.round(x / 1000)}k`;
  return fmtInt(x);
}

export function fmtDuration(ms: number | null | undefined): string {
  if (ms == null) return "n/a";
  if (ms < 1000) return `${Math.round(ms)} ms`;
  if (ms < 60_000) return `${(ms / 1000).toFixed(1)} s`;
  return `${Math.floor(ms / 60_000)} m ${Math.round((ms % 60_000) / 1000)} s`;
}

export function fmtUptime(s: number): string {
  const d = Math.floor(s / 86400);
  const h = Math.floor((s % 86400) / 3600);
  const m = Math.floor((s % 3600) / 60);
  return d ? `${d} d ${h} h` : h ? `${h} h ${m} m` : `${m} m`;
}

/** Share of `part` in `whole` as 0..100, or 0 when `whole` is empty. */
export const pctOf = (part: number, whole: number) => (whole > 0 ? Math.min(100, Math.max(0, (100 * part) / whole)) : 0);
