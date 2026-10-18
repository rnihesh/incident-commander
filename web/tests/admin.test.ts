import { describe, expect, it } from "vitest";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { AdminView } from "../components/AdminPanel";
import { AdminError, fmtDuration, fmtTokens, fmtUsd, getAdminRuns, getAdminSummary, type AdminRun, type AdminSummary } from "../lib/admin";

const spend = (usd: number) => ({
  usd, calls: 3, prompt_tokens: 12_000, completion_tokens: 800,
  by_model: usd ? [{ model: "gpt-6-luna", calls: 3, prompt_tokens: 12_000, completion_tokens: 800, usd }] : [],
});
const runStats = (total: number) => ({
  total, degraded: 1, prs: 1, errors: 0, usd: 0.0021, tokens: 12_800, avg_duration_ms: 8400,
  by_verdict: { DIAGNOSED: total }, by_fault_class: { bad_deploy: total }, by_kind: { investigation: total },
});
const SUMMARY: AdminSummary = {
  spend: { today: spend(0.0021), last_7_days: spend(0.31), all_time: spend(1.2) },
  runs: { today: runStats(2), last_7_days: runStats(9) },
  daily: ["2026-10-12", "2026-10-13", "2026-10-14", "2026-10-15", "2026-10-16", "2026-10-17", "2026-10-18"].map((day, i) => ({
    day, usd: i * 0.01, tokens: i * 1000, runs: i, degraded_runs: 0, sessions: i, asks: 0, clients: i, refusals: i === 6 ? 2 : 0,
  })),
  sessions: { active: 3, max: 8, seen_last_5_min: 2, created_today: 5, runs_in_flight: 1 },
  clients: { unique_today: 4, top_today: [{ client: "a1b2c3d4", events: 7 }] },
  activity_today: { runs_started: 2, asks: 6, verifies: 1, proof_verifies: 0, fragility: 0, challenges: 1 },
  refusals_today: { total: 2, by_kind: { run_session: 2 } },
  admin_auth_failures_today: 0,
  breaker: { daily_usd: 0.5, spent_today: 0.0021, remaining_usd: 0.4979, tripped: false },
  falkordb: {
    reachable: true, used_memory: 400_000_000, used_memory_human: "381.47M", peak_memory_human: "420.00M",
    maxmemory: 2_147_483_648, maxmemory_human: "2.00G", maxmemory_policy: "noeviction", used_pct: 18.6,
    graphs: { total: 14, session: 12, pr: 1, bench: 0, other: 1 },
  },
  process: { started_at: "2026-10-18T08:00:00Z", uptime_s: 7260 },
  config: { llm: { provider: "openai", model: "gpt-6-luna", configured: true }, limits: { max_sessions: 8, daily_usd_breaker: 0.5, session_idle_ttl_s: 1800 } },
  generated_at: "2026-10-18T10:01:00Z",
  day_basis: "UTC",
};
const RUN: AdminRun = {
  run_id: "run-1", time: "2026-10-18T09:58:12Z", kind: "investigation", incident_id: "inc-7", fault_class: "bad_deploy",
  verdict: "DIAGNOSED", degraded: false, degraded_reason: null, final_state: "LEARNED", tool_calls: 11,
  prompt_tokens: 12_000, completion_tokens: 800, tokens: 12_800, usd: 0.0021, duration_ms: 8400, pr: true, simulated: true, error: null,
};

const ok = (body: unknown) => async () => new Response(JSON.stringify(body), { status: 200 });

describe("admin api", () => {
  it("sends the bearer token and no cookies", async () => {
    let url = "";
    let init: RequestInit | undefined;
    await getAdminSummary("tok", async (u, i) => { url = u; init = i; return new Response("{}", { status: 200 }); });
    expect(url).toMatch(/\/api\/admin\/summary$/);
    expect(init?.credentials).toBe("omit");
    expect((init?.headers as Record<string, string>).Authorization).toBe("Bearer tok");
  });
  it("maps 404, 401 and 429 to AdminError with a readable message", async () => {
    const fail = (status: number, headers: Record<string, string> = {}) => async () => new Response("{}", { status, headers });
    const off = await getAdminSummary("t", fail(404)).catch((e) => e);
    expect(off).toBeInstanceOf(AdminError);
    expect(off.message).toMatch(/IC_ADMIN_TOKEN/);
    expect((await getAdminSummary("t", fail(401)).catch((e) => e)).status).toBe(401);
    expect((await getAdminSummary("t", fail(429, { "retry-after": "120" })).catch((e) => e)).message).toMatch(/120 s/);
  });
  it("unwraps the run list", async () => {
    expect(await getAdminRuns("t", 5, ok({ runs: [RUN], limit: 5 }))).toEqual([RUN]);
  });
  it("formats money, tokens and durations", () => {
    expect([fmtUsd(0), fmtUsd(0.0021), fmtUsd(1.234)]).toEqual(["$0", "$0.0021", "$1.23"]);
    expect([fmtTokens(950), fmtTokens(12_800), fmtTokens(2_500_000)]).toEqual(["950", "13k", "2.5M"]);
    expect([fmtDuration(420), fmtDuration(8400), fmtDuration(125_000)]).toEqual(["420 ms", "8.4 s", "2 m 5 s"]);
  });
});

describe("admin view", () => {
  it("renders the cards, the 7-day table and the recent runs", () => {
    const html = renderToStaticMarkup(createElement(AdminView, { summary: SUMMARY, runs: [RUN] }));
    for (const text of ["LLM spend", "Runs", "Sessions", "FalkorDB", "Last 7 days", "Recent runs", "Configured limits"]) {
      expect(html).toContain(text);
    }
    expect(html).toContain("$0.0021");
    expect(html).toContain("3 / 8");
    expect(html).toContain("381.47M");
    expect(html).toContain("2026-10-12");
    expect(html).toContain("inc-7");
    expect(html).toContain("PR (simulated)");
    expect(html).toContain("run_session");
  });
  it("shows an unreachable FalkorDB and an empty run list plainly", () => {
    const html = renderToStaticMarkup(createElement(AdminView, { summary: { ...SUMMARY, falkordb: { reachable: false, error: "TimeoutError" } }, runs: [] }));
    expect(html).toContain("UNREACHABLE");
    expect(html).toContain("No runs recorded yet.");
  });
});
