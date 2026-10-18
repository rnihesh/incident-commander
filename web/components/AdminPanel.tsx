"use client";

import { useCallback, useEffect, useState, useSyncExternalStore, type FormEvent, type ReactNode } from "react";
import {
  ADMIN_REFRESH_MS,
  ADMIN_TOKEN_KEY,
  AdminError,
  fmtDuration,
  fmtInt,
  fmtTokens,
  fmtUptime,
  fmtUsd,
  getAdminRuns,
  getAdminSummary,
  pctOf,
  type AdminRun,
  type AdminSummary,
} from "../lib/admin";
import { PageHeader } from "./Shell";
import { Badge, Button, Field, Input, Panel, PanelHead, Placeholder, type Tone } from "./ui";

const TH = "px-inset py-2 text-left align-bottom text-meta font-medium text-dim";
const THR = `${TH} text-right`;
const TD = "px-inset py-1.5 text-label text-ink";
const TDR = `${TD} text-right font-mono tabular-nums`;

// --- token in sessionStorage, read without a hydration mismatch -----------------------------

const TOKEN_EVENT = "ic-admin-token";
const readToken = () => {
  try {
    return window.sessionStorage.getItem(ADMIN_TOKEN_KEY) ?? "";
  } catch {
    return "";
  }
};
const subscribeToken = (cb: () => void) => {
  window.addEventListener(TOKEN_EVENT, cb);
  return () => window.removeEventListener(TOKEN_EVENT, cb);
};
function storeToken(token: string) {
  try {
    if (token) window.sessionStorage.setItem(ADMIN_TOKEN_KEY, token);
    else window.sessionStorage.removeItem(ADMIN_TOKEN_KEY);
  } catch {
    /* storage blocked: the token then lasts only until reload */
  }
  window.dispatchEvent(new Event(TOKEN_EVENT));
}

// --- pieces ------------------------------------------------------------------------------------

function Stat({ label, value, sub }: { label: string; value: ReactNode; sub?: ReactNode }) {
  return (
    <div className="min-w-0">
      <p className="text-meta text-dim">{label}</p>
      <p className="font-mono text-display tabular-nums text-ink">{value}</p>
      {sub && <p className="text-meta text-faint">{sub}</p>}
    </div>
  );
}

function Meter({ pct, tone = "info", label }: { pct: number; tone?: Tone; label: string }) {
  const fill = tone === "falsified" ? "bg-falsified" : tone === "drifted" ? "bg-drifted" : "bg-symptom";
  return (
    <div className="h-1.5 w-full bg-raise" role="meter" aria-label={label} aria-valuemin={0} aria-valuemax={100} aria-valuenow={Math.round(pct)}>
      <div className={`h-full ${fill}`} style={{ width: `${pct}%` }} />
    </div>
  );
}

const meterTone = (pct: number): Tone => (pct >= 90 ? "falsified" : pct >= 70 ? "drifted" : "info");

function Counts({ data, empty = "none" }: { data: Record<string, number>; empty?: string }) {
  const entries = Object.entries(data);
  if (!entries.length) return <span className="text-meta text-faint">{empty}</span>;
  return (
    <ul className="flex flex-wrap gap-1.5">
      {entries.map(([k, n]) => (
        <li key={k} className="inline-flex h-6 items-center gap-1.5 rounded-ctl border border-rule-hi px-2 font-mono text-meta text-dim">
          {k}
          <span className="text-ink tabular-nums">{n}</span>
        </li>
      ))}
    </ul>
  );
}

function Card({ title, meta, children }: { title: string; meta?: ReactNode; children: ReactNode }) {
  return (
    <Panel className="flex flex-col">
      <PanelHead title={title} meta={meta} />
      <div className="flex flex-1 flex-col gap-3 px-inset py-3">{children}</div>
    </Panel>
  );
}

function SpendCard({ s }: { s: AdminSummary }) {
  const { today, last_7_days: week, all_time: all } = s.spend;
  const pct = pctOf(s.breaker.spent_today, s.breaker.daily_usd);
  return (
    <Card title="LLM spend" meta={s.config.llm.configured ? `${s.config.llm.provider} / ${s.config.llm.model ?? "default"}` : "no LLM key: deterministic fallback"}>
      <Stat label="Today" value={fmtUsd(today.usd)} sub={`${fmtTokens(today.prompt_tokens + today.completion_tokens)} tokens, ${fmtInt(today.calls)} calls`} />
      <div className="flex flex-col gap-1">
        <Meter pct={pct} tone={s.breaker.tripped ? "falsified" : meterTone(pct)} label="Share of the daily spend cap used" />
        <p className="text-meta text-faint">
          {s.breaker.tripped ? "Breaker tripped: serving recorded runs. " : ""}
          {fmtUsd(s.breaker.remaining_usd)} left of the {fmtUsd(s.breaker.daily_usd)} daily cap
        </p>
      </div>
      <div className="grid grid-cols-2 gap-3">
        <Stat label="Last 7 days" value={fmtUsd(week.usd)} sub={`${fmtTokens(week.prompt_tokens + week.completion_tokens)} tokens`} />
        <Stat label="All time" value={fmtUsd(all.usd)} sub={`${fmtTokens(all.prompt_tokens + all.completion_tokens)} tokens`} />
      </div>
      {week.by_model.length > 0 && (
        <table className="w-full border-collapse">
          <thead className="border-b border-rule">
            <tr>
              <th scope="col" className={`${TH} px-0`}>Model (7 d)</th>
              <th scope="col" className={THR}>Tokens in / out</th>
              <th scope="col" className={`${THR} pr-0`}>USD</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-rule/70">
            {week.by_model.map((m) => (
              <tr key={m.model}>
                <td className={`${TD} px-0 font-mono`}>{m.model}</td>
                <td className={TDR}>{fmtTokens(m.prompt_tokens)} / {fmtTokens(m.completion_tokens)}</td>
                <td className={`${TDR} pr-0`}>{fmtUsd(m.usd)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </Card>
  );
}

function RunsCard({ s }: { s: AdminSummary }) {
  const { today, last_7_days: week } = s.runs;
  return (
    <Card title="Runs" meta={`${s.sessions.runs_in_flight} in flight`}>
      <div className="grid grid-cols-2 gap-3">
        <Stat label="Today" value={fmtInt(today.total)} sub={`${today.degraded} degraded, ${today.prs} PRs, ${today.errors} errors`} />
        <Stat label="Last 7 days" value={fmtInt(week.total)} sub={`avg ${fmtDuration(week.avg_duration_ms)}`} />
      </div>
      <div className="flex flex-col gap-1.5">
        <p className="text-meta text-dim">Verdicts today</p>
        <Counts data={today.by_verdict} />
        <p className="text-meta text-dim">Fault classes today</p>
        <Counts data={today.by_fault_class} />
        <p className="text-meta text-dim">Kinds today</p>
        <Counts data={today.by_kind} />
      </div>
    </Card>
  );
}

function SessionsCard({ s }: { s: AdminSummary }) {
  const pct = pctOf(s.sessions.active, s.sessions.max);
  return (
    <Card title="Sessions" meta={`idle TTL ${fmtUptime(s.config.limits.session_idle_ttl_s ?? 0)}`}>
      <Stat label="Active" value={`${s.sessions.active} / ${s.sessions.max}`} sub={`${s.sessions.seen_last_5_min} seen in the last 5 min`} />
      <Meter pct={pct} tone={meterTone(pct)} label="Share of session slots in use" />
      <div className="grid grid-cols-2 gap-3">
        <Stat label="Created today" value={fmtInt(s.sessions.created_today)} />
        <Stat label="Unique clients today" value={fmtInt(s.clients.unique_today)} />
      </div>
      {s.clients.top_today.length > 0 && (
        <div className="flex flex-col gap-1.5">
          <p className="text-meta text-dim" title="Salted hash prefixes; addresses are never stored">Busiest clients today</p>
          <Counts data={Object.fromEntries(s.clients.top_today.map((c) => [c.client, c.events]))} />
        </div>
      )}
    </Card>
  );
}

function FalkorCard({ s }: { s: AdminSummary }) {
  const f = s.falkordb;
  if (!f.reachable) {
    return (
      <Card title="FalkorDB">
        <Badge tone="falsified">UNREACHABLE</Badge>
        <p className="text-meta text-faint">{f.error ?? "no answer"}</p>
      </Card>
    );
  }
  const pct = f.used_pct ?? 0;
  return (
    <Card title="FalkorDB" meta={f.maxmemory_policy ?? undefined}>
      <Stat
        label="Memory used"
        value={f.used_memory_human ?? fmtInt(f.used_memory)}
        sub={f.maxmemory ? `of ${f.maxmemory_human} (${pct}%), peak ${f.peak_memory_human}` : `no maxmemory set, peak ${f.peak_memory_human}`}
      />
      {f.maxmemory ? <Meter pct={pct} tone={meterTone(pct)} label="Share of FalkorDB maxmemory used" /> : null}
      <Stat label="Graphs" value={fmtInt(f.graphs?.total)} />
      {f.graphs && <Counts data={{ session: f.graphs.session, pr: f.graphs.pr, bench: f.graphs.bench, other: f.graphs.other }} />}
      <p className="text-meta text-faint">API up {fmtUptime(s.process.uptime_s)}</p>
    </Card>
  );
}

function DailyTable({ s }: { s: AdminSummary }) {
  const maxUsd = Math.max(...s.daily.map((d) => d.usd), 0);
  const maxRuns = Math.max(...s.daily.map((d) => d.runs), 0);
  return (
    <Panel>
      <PanelHead title="Last 7 days" meta={`days in ${s.day_basis}`} />
      <div className="overflow-x-auto">
        <table className="w-full border-collapse">
          <thead className="border-b border-rule">
            <tr>
              <th scope="col" className={TH}>Day</th>
              <th scope="col" className={TH}>Spend</th>
              <th scope="col" className={THR}>Tokens</th>
              <th scope="col" className={TH}>Runs</th>
              <th scope="col" className={THR}>Degraded</th>
              <th scope="col" className={THR}>Sessions</th>
              <th scope="col" className={THR}>Clients</th>
              <th scope="col" className={THR}>Asks</th>
              <th scope="col" className={THR}>Refusals</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-rule/70">
            {s.daily.map((d) => (
              <tr key={d.day}>
                <td className={`${TD} font-mono`}>{d.day}</td>
                <td className={`${TD} w-[22%]`}>
                  <div className="flex items-center gap-2">
                    <span className="w-16 shrink-0 font-mono tabular-nums">{fmtUsd(d.usd)}</span>
                    <div className="h-1.5 flex-1 bg-raise" aria-hidden>
                      <div className="h-full bg-symptom" style={{ width: `${pctOf(d.usd, maxUsd)}%` }} />
                    </div>
                  </div>
                </td>
                <td className={TDR}>{fmtTokens(d.tokens)}</td>
                <td className={`${TD} w-[18%]`}>
                  <div className="flex items-center gap-2">
                    <span className="w-8 shrink-0 font-mono tabular-nums">{d.runs}</span>
                    <div className="h-1.5 flex-1 bg-raise" aria-hidden>
                      <div className="h-full bg-verified" style={{ width: `${pctOf(d.runs, maxRuns)}%` }} />
                    </div>
                  </div>
                </td>
                <td className={TDR}>{d.degraded_runs}</td>
                <td className={TDR}>{d.sessions}</td>
                <td className={TDR}>{d.clients}</td>
                <td className={TDR}>{d.asks}</td>
                <td className={TDR}>{d.refusals}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Panel>
  );
}

const ACTIVITY_LABELS: Record<string, string> = {
  runs_started: "Runs started",
  asks: "Ask questions",
  verifies: "Verifies",
  proof_verifies: "Proof page re-verifies",
  fragility: "Fragility maps",
  challenges: "Judge challenges",
};

function ActivityPanel({ s }: { s: AdminSummary }) {
  return (
    <Panel>
      <PanelHead title="Today" meta={`${s.refusals_today.total} refusals, ${s.admin_auth_failures_today} failed admin logins`} />
      <div className="grid gap-4 px-inset py-3 sm:grid-cols-2">
        <dl className="grid grid-cols-[1fr_auto] gap-x-4 gap-y-1 text-label">
          {Object.entries(ACTIVITY_LABELS).map(([k, label]) => (
            <div key={k} className="contents">
              <dt className="text-dim">{label}</dt>
              <dd className="text-right font-mono tabular-nums text-ink">{fmtInt(s.activity_today[k] ?? 0)}</dd>
            </div>
          ))}
        </dl>
        <div className="flex flex-col gap-1.5">
          <p className="text-meta text-dim">Refusals by limit</p>
          <Counts data={s.refusals_today.by_kind} empty="none today" />
        </div>
      </div>
    </Panel>
  );
}

const LIMIT_LABELS: Record<string, string> = {
  max_sessions: "Concurrent sessions",
  session_runs_per_hour: "Runs / session / h",
  ip_concurrent_sessions: "Sessions / client",
  ip_sessions_per_hour: "New sessions / client / h",
  ip_runs_per_hour: "Runs / client / h",
  global_runs_per_hour: "Runs / h (global)",
  actions_per_hour: "PRs and pages / h",
  session_asks_per_hour: "Asks / session / h",
  session_verifies_per_hour: "Verifies / session / h",
  session_fragility_per_hour: "Fragility maps / session / h",
  proof_verifies_per_ip_hour: "Proof re-verifies / client / h",
  daily_usd_breaker: "Daily spend cap (USD)",
};

function LimitsPanel({ s }: { s: AdminSummary }) {
  return (
    <Panel>
      <PanelHead title="Configured limits" meta={`LLM: ${s.config.llm.provider}${s.config.llm.model ? ` / ${s.config.llm.model}` : ""}${s.config.llm.configured ? "" : " (no key)"}`} />
      <dl className="grid grid-cols-[1fr_auto] gap-x-4 gap-y-1 px-inset py-3 text-label sm:grid-cols-[1fr_auto_1fr_auto]">
        {Object.entries(LIMIT_LABELS).map(([k, label]) => (
          <div key={k} className="contents">
            <dt className="text-dim">{label}</dt>
            <dd className="text-right font-mono tabular-nums text-ink">{s.config.limits[k] ?? "n/a"}</dd>
          </div>
        ))}
      </dl>
    </Panel>
  );
}

const VERDICT_TONE: Record<string, Tone> = { DIAGNOSED: "verified", INCONCLUSIVE: "drifted", FAILED: "falsified" };

export function RunsTable({ runs }: { runs: AdminRun[] }) {
  return (
    <Panel>
      <PanelHead title="Recent runs" meta={`newest first, ${runs.length} shown`} />
      {runs.length === 0 ? (
        <Placeholder>No runs recorded yet.</Placeholder>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full border-collapse">
            <thead className="border-b border-rule">
              <tr>
                <th scope="col" className={TH}>Started (UTC)</th>
                <th scope="col" className={TH}>Incident</th>
                <th scope="col" className={TH}>Kind</th>
                <th scope="col" className={TH}>Fault class</th>
                <th scope="col" className={TH}>Verdict</th>
                <th scope="col" className={TH}>Final state</th>
                <th scope="col" className={THR}>Tool calls</th>
                <th scope="col" className={THR}>Tokens</th>
                <th scope="col" className={THR}>Cost</th>
                <th scope="col" className={THR}>Duration</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-rule/70">
              {runs.map((r) => (
                <tr key={r.run_id} title={r.error ?? undefined}>
                  <td className={`${TD} whitespace-nowrap font-mono`}>{r.time.replace("T", " ").replace("Z", "")}</td>
                  <td className={`${TD} font-mono`}>{r.incident_id}</td>
                  <td className={`${TD} text-dim`}>{r.kind}</td>
                  <td className={`${TD} font-mono`}>{r.fault_class ?? "n/a"}</td>
                  <td className={TD}>
                    <span className="inline-flex flex-wrap gap-1">
                      {r.verdict ? <Badge tone={VERDICT_TONE[r.verdict] ?? "muted"}>{r.verdict}</Badge> : <Badge tone="muted">{r.error ? "ERROR" : "NONE"}</Badge>}
                      {r.degraded && <Badge tone="degraded" title={r.degraded_reason ?? undefined}>DEGRADED</Badge>}
                      {r.pr && <Badge tone="info">{r.simulated ? "PR (simulated)" : "PR"}</Badge>}
                    </span>
                  </td>
                  <td className={`${TD} font-mono text-dim`}>{r.final_state ?? "n/a"}</td>
                  <td className={TDR}>{r.tool_calls}</td>
                  <td className={TDR}>{fmtTokens(r.tokens)}</td>
                  <td className={TDR}>{fmtUsd(r.usd)}</td>
                  <td className={TDR}>{fmtDuration(r.duration_ms)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Panel>
  );
}

/** The whole panel for one summary and run list (no fetching; rendered in tests). */
export function AdminView({ summary, runs }: { summary: AdminSummary; runs: AdminRun[] }) {
  return (
    <div className="flex flex-col gap-gutter">
      <div className="grid gap-gutter md:grid-cols-2 xl:grid-cols-4">
        <SpendCard s={summary} />
        <RunsCard s={summary} />
        <SessionsCard s={summary} />
        <FalkorCard s={summary} />
      </div>
      <DailyTable s={summary} />
      <div className="grid gap-gutter lg:grid-cols-2">
        <ActivityPanel s={summary} />
        <LimitsPanel s={summary} />
      </div>
      <RunsTable runs={runs} />
    </div>
  );
}

function TokenForm({ onSubmit, error }: { onSubmit: (token: string) => void; error: string | null }) {
  const [value, setValue] = useState("");
  const submit = (e: FormEvent) => {
    e.preventDefault();
    if (value.trim()) onSubmit(value.trim());
  };
  return (
    <Panel className="mx-auto max-w-md">
      <PanelHead title="Admin token" />
      <form onSubmit={submit} className="flex flex-col gap-3 px-inset py-3">
        <Field label="Bearer token (IC_ADMIN_TOKEN)" htmlFor="admin-token" hint="Kept in this tab's session storage only.">
          <Input id="admin-token" type="password" autoComplete="off" value={value} onChange={(e) => setValue(e.target.value)} className="w-full" />
        </Field>
        {error && <p className="text-label text-falsified" role="alert">{error}</p>}
        <div>
          <Button type="submit" variant="primary" disabled={!value.trim()}>Open panel</Button>
        </div>
      </form>
    </Panel>
  );
}

export function AdminPanel() {
  const token = useSyncExternalStore(subscribeToken, readToken, () => "");
  const [summary, setSummary] = useState<AdminSummary | null>(null);
  const [runs, setRuns] = useState<AdminRun[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [auto, setAuto] = useState(true);

  const load = useCallback(async (t: string) => {
    setLoading(true);
    try {
      const [s, r] = await Promise.all([getAdminSummary(t), getAdminRuns(t, 50)]);
      setSummary(s);
      setRuns(r);
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : "request failed");
      if (e instanceof AdminError && (e.status === 401 || e.status === 404)) {
        setSummary(null);
        storeToken("");
      }
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    if (!token) return;
    const first = setTimeout(() => void load(token), 0);
    const id = auto ? setInterval(() => void load(token), ADMIN_REFRESH_MS) : undefined;
    return () => {
      clearTimeout(first);
      clearInterval(id);
    };
  }, [token, auto, load]);

  const signedIn = Boolean(token);
  return (
    <div className="h-screen overflow-y-auto">
      <PageHeader
        sticky
        title="Admin"
        status={
          summary && (
            <>
              <Badge tone={summary.breaker.tripped ? "falsified" : "verified"}>{summary.breaker.tripped ? "BREAKER TRIPPED" : "BREAKER OK"}</Badge>
              <Badge tone="muted">updated {summary.generated_at.slice(11, 19)} UTC</Badge>
            </>
          )
        }
        right={
          signedIn && (
            <>
              <label className="flex items-center gap-1.5 text-meta text-dim">
                <input type="checkbox" checked={auto} onChange={(e) => setAuto(e.target.checked)} />
                Auto-refresh 30 s
              </label>
              <Button size="sm" onClick={() => void load(token)} disabled={loading}>{loading ? "Refreshing" : "Refresh"}</Button>
              <Button size="sm" variant="ghost" onClick={() => { storeToken(""); setSummary(null); setRuns([]); }}>Forget token</Button>
            </>
          )
        }
      />
      <main className="mx-auto max-w-[1600px] px-4 py-6 sm:px-8">
        {!signedIn ? (
          <TokenForm onSubmit={storeToken} error={error} />
        ) : summary ? (
          <>
            {error && <p className="mb-3 text-label text-drifted" role="alert">Last refresh failed: {error}</p>}
            <AdminView summary={summary} runs={runs} />
          </>
        ) : (
          <p className="text-body text-faint" role={error ? "alert" : "status"}>{error ?? "Loading usage."}</p>
        )}
      </main>
    </div>
  );
}
