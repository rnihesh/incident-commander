"use client";

import { useEffect, useState, type ReactNode } from "react";
import { getBenchmark, type BenchLatency, type BenchLiveRun, type BenchmarkResponse, type BenchScale, type BenchWhyRow } from "../lib/api";
import { LearningCurveChart } from "./LearningCurve";
import { Badge, type Tone } from "./ui";
import { PageHeader, PageNav } from "./Shell";

const pct = (x: number | null | undefined) => (x == null ? "n/a" : `${Math.round(x * 100)}%`);
const ms = (x: number | null | undefined, digits = 0) => (x == null ? "n/a" : x.toFixed(digits));
const int = (x: number | null | undefined) => (x == null ? "n/a" : Math.round(x).toLocaleString("en-US"));

/** Approaches in report order (meta.approaches), falling back to the summary's keys. */
export function approachOrder(data: BenchmarkResponse): string[] {
  const names = (data.meta.approaches ?? []).map((a) => a.name).filter((n) => n in data.summary);
  return names.length ? names : Object.keys(data.summary);
}

/** Plain statement of what the accuracy numbers can and cannot show. */
export function honestyNotes(data: BenchmarkResponse): string[] {
  const llm = data.meta.llm ?? "";
  const noLlm = /^none/i.test(llm);
  const live = (data.live_llm_runs ?? []).length > 0;
  const out: string[] = [];
  out.push(
    noLlm && live
      ? "The headline deterministic table above has no LLM: graph is the deterministic fallback (funnel plus proof chain, same verdict gate); vector_rag and full_context are heuristic stand-ins, and its token columns are 0 or estimates. Runs with a real language model are in the Live LLM runs table."
      : noLlm
      ? "No LLM key was configured for this run. graph is the deterministic fallback (funnel plus proof chain, same verdict gate); vector_rag and full_context are heuristic stand-ins, not language models. Token columns are therefore 0 or estimates."
      : `Run with ${llm}.`,
  );
  out.push(
    "Graph accuracy is partly by construction: every injected fault is verified, when it is planted, against the same causal rule (KTD23) the graph approach screens with. A perfect score shows the funnel and proofs implement that rule end to end on these worlds; it is not evidence about real incidents that break the rule.",
  );
  return out;
}

/** One table row per (live run, approach): correct counts out of cases, held-out, tokens. */
export function liveRunRows(runs: BenchLiveRun[]) {
  return runs.flatMap((run) => {
    const order = (run.approaches ?? []).filter((a) => a in run.summary);
    for (const a of Object.keys(run.summary)) if (!order.includes(a)) order.push(a);
    return order.map((approach, i) => {
      const s = run.summary[approach];
      return {
        key: `${run.model}:${run.cases}:${approach}`,
        model: run.model,
        cases: run.cases,
        approach,
        first: i === 0,
        span: Object.keys(run.summary).length,
        correct: s.correct,
        n: s.n,
        overall: s.accuracy_overall ?? (s.n ? s.correct / s.n : null),
        heldout: s.accuracy_heldout ?? null,
        heldoutN: run.n_heldout,
        tokens: s.mean_llm_tokens ?? null,
        fallback: s.fallback ?? null,
      };
    });
  });
}

function LiveRunsTable({ runs }: { runs: BenchLiveRun[] }) {
  const rows = liveRunRows(runs);
  return (
    <table className="w-full border-collapse">
      <thead className="border-b border-rule">
        <tr>
          <th scope="col" className={TH}>Model</th>
          <th scope="col" className={THR}>Cases</th>
          <th scope="col" className={TH}>Approach</th>
          <th scope="col" className={THR}>Overall correct</th>
          <th scope="col" className={THR}>Held-out</th>
          <th scope="col" className={THR}>LLM tokens / case</th>
          <th scope="col" className={THR} title="Verdicts the deterministic funnel produced after the model failed (timeout, step cap)">Fallback</th>
        </tr>
      </thead>
      <tbody className="divide-y divide-rule/70">
        {rows.map((r) => (
          <tr key={r.key} data-live-run={r.model} data-approach={r.approach}>
            <th scope="row" className={`${TD} text-left font-mono font-semibold`}>{r.first ? r.model : ""}</th>
            <td className={TDR}>{r.first ? r.cases : ""}</td>
            <td className={`${TD} font-mono`}>{r.approach}</td>
            <td className={TDR}>
              {pct(r.overall)} <span className="text-label text-dim">({r.correct}/{r.n})</span>
            </td>
            <td className={TDR}>{pct(r.heldout)}</td>
            <td className={TDR}>{int(r.tokens)}</td>
            <td className={TDR}>{r.fallback == null ? "-" : `${r.fallback}/${r.n}`}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

/** Funnel clauses in the order they are checked, then the verdicts that are not a clause. */
const WHY_ORDER = [
  "after_onset", "origin", "coverage", "order", "signature", "outside_window", "not_a_change",
  "rival", "verified", "unknown_id", "cannot_regenerate", "inconsistent", "no_claim",
];
const whyRank = (b: string) => (WHY_ORDER.includes(b) ? WHY_ORDER.indexOf(b) : WHY_ORDER.length);
const whyLabel = (b: string) => b.replace(/_/g, " ");

/** The failing clause of a REFUTED row, else the row's verdict (as in report.py). */
export const whyBucket = (r: BenchWhyRow) => (r.verdict === "REFUTED" && r.clause ? r.clause : r.verdict.toLowerCase());

const whyTone = (b: string): Tone =>
  b === "no_claim" ? "muted" : b === "cannot_regenerate" || b === "unknown_id" || b === "inconsistent" ? "degraded" : b === "rival" || b === "verified" ? "drifted" : "falsified";

/** Per approach (in row order): wrong count and [bucket, count] pairs, funnel order first. */
export function whyCounts(rows: BenchWhyRow[]) {
  const by = new Map<string, Map<string, number>>();
  for (const r of rows) {
    const m = by.get(r.approach) ?? new Map<string, number>();
    by.set(r.approach, m);
    m.set(whyBucket(r), (m.get(whyBucket(r)) ?? 0) + 1);
  }
  return [...by].map(([approach, m]) => ({
    approach,
    total: [...m.values()].reduce((a, b) => a + b, 0),
    counts: [...m].sort(([a], [b]) => whyRank(a) - whyRank(b) || a.localeCompare(b)),
  }));
}

/** Collapsible per live run: clause chips per approach, then one row per wrong prediction. */
function WhyWrongRun({ run }: { run: BenchLiveRun }) {
  const rows = run.why_wrong ?? [];
  return (
    <details className="border-t border-rule" data-why-run={run.model}>
      <summary className="cursor-pointer px-inset py-2.5 text-body text-ink hover:text-symptom">
        <span className="font-semibold">Why it was wrong</span>
        <span className="ml-2 text-label text-dim">
          {run.model} · {run.cases} cases · {rows.length} wrong prediction{rows.length === 1 ? "" : "s"} re-checked with verify_claim
        </span>
      </summary>
      <div className="space-y-1.5 px-inset pb-3">
        {whyCounts(rows).map((g) => (
          <div key={g.approach} className="flex flex-wrap items-center gap-1.5" data-why-approach={g.approach}>
            <span className="w-28 font-mono text-label text-ink">{g.approach}</span>
            <span className="mr-1 font-mono text-label text-dim">{g.total} wrong</span>
            {g.counts.map(([b, n]) => (
              <span key={b} data-why-chip={b}>
                <Badge tone={whyTone(b)}>
                  {whyLabel(b)} {n}
                </Badge>
              </span>
            ))}
          </div>
        ))}
      </div>
      <table className="w-full border-collapse border-t border-rule">
        <thead className="border-b border-rule">
          <tr>
            <th scope="col" className={TH}>Case</th>
            <th scope="col" className={TH}>Fault class</th>
            <th scope="col" className={TH}>Approach</th>
            <th scope="col" className={TH}>Predicted</th>
            <th scope="col" className={TH}>Verdict</th>
            <th scope="col" className={TH}>Why</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-rule/70">
          {rows.map((r) => {
            const b = whyBucket(r);
            return (
              <tr key={`${r.case}:${r.approach}`} className="align-top" data-why-row={b}>
                <td className={`${TD} font-mono text-label`}>{r.case}</td>
                <td className={`${TD} font-mono text-label`}>
                  {r.fault_class}
                  {r.held_out && <span className="block text-meta text-late">held-out</span>}
                </td>
                <td className={`${TD} font-mono text-label`}>{r.approach}</td>
                <td className={`${TD} font-mono text-label`}>{r.predicted ?? <span className="text-faint">none</span>}</td>
                <td className={TD}>
                  <Badge tone={whyTone(b)}>{r.verdict === "REFUTED" ? `REFUTED · ${whyLabel(b)}` : whyLabel(b)}</Badge>
                </td>
                <td className={`${TD} text-label text-dim`}>
                  {r.detail ?? (b === "no_claim" ? "the approach named no root cause" : "")}
                  {r.query_text && (
                    <details className="mt-1">
                      <summary className="cursor-pointer font-mono text-meta text-faint hover:text-symptom">deciding query</summary>
                      <pre className="mt-1 max-h-64 overflow-auto whitespace-pre border border-rule bg-raise p-2 font-mono text-meta leading-snug text-ink/90">
                        {r.query_text}
                      </pre>
                    </details>
                  )}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </details>
  );
}

function Section({ title, meta, children, className = "" }: { title: string; meta?: ReactNode; children: ReactNode; className?: string }) {
  return (
    <section className={`border border-rule bg-deck ${className}`}>
      <div className="flex flex-wrap items-baseline gap-x-3 gap-y-0.5 border-b border-rule px-inset py-2.5">
        <h2 className="text-title font-semibold tracking-tight text-ink">{title}</h2>
        {meta && <span className="text-label text-dim">{meta}</span>}
      </div>
      {/* Wide tables scroll inside their section, never the page. */}
      <div className="min-w-0 overflow-x-auto">{children}</div>
    </section>
  );
}

const TH = "px-inset py-2 text-left align-bottom text-meta font-medium text-dim";
const THR = `${TH} text-right`;
const TD = "px-inset py-2 text-body text-ink";
const TDR = `${TD} text-right font-mono tabular-nums`;

function AccuracyTable({ data }: { data: BenchmarkResponse }) {
  const order = approachOrder(data);
  const best = Math.max(...order.map((n) => data.summary[n].accuracy_overall ?? 0));
  return (
    <table className="w-full border-collapse">
      <thead className="border-b border-rule">
        <tr>
          <th scope="col" className={TH}>Approach</th>
          <th scope="col" className={THR}>Accuracy seen ({data.meta.n_seen})</th>
          <th scope="col" className={THR}>Held-out ({data.meta.n_heldout})</th>
          <th scope="col" className={THR}>Overall</th>
          <th scope="col" className={THR}>Mean ms / case</th>
          <th scope="col" className={THR}>LLM tokens / case</th>
          <th scope="col" className={THR}>Prompt tokens (est.)</th>
          <th scope="col" className={THR}>Tool calls / case</th>
        </tr>
      </thead>
      <tbody className="divide-y divide-rule/70">
        {order.map((name) => {
          const s = data.summary[name];
          const top = (s.accuracy_overall ?? 0) === best;
          return (
            <tr key={name} data-approach={name}>
              <th scope="row" className={`${TD} text-left font-normal`}>
                <span className="font-mono font-semibold">{name}</span>
                <span className="ml-2 text-label text-dim">{s.label.replace(name, "").replace(/^\s*\(?|\)?\s*$/g, "")}</span>
              </th>
              <td className={TDR}>{pct(s.accuracy_seen)}</td>
              <td className={TDR}>{pct(s.accuracy_heldout)}</td>
              <td className={`${TDR} ${top ? "text-verified" : ""}`}>
                {pct(s.accuracy_overall)} <span className="text-label text-dim">({s.correct}/{s.n})</span>
              </td>
              <td className={TDR}>{ms(s.mean_ms)}</td>
              <td className={TDR}>{int(s.mean_llm_tokens)}</td>
              <td className={TDR}>{int(s.mean_prompt_tokens_est)}</td>
              <td className={TDR}>{s.mean_tool_calls.toFixed(1)}</td>
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}

function PerClassTable({ data }: { data: BenchmarkResponse }) {
  const order = approachOrder(data);
  const classes = data.meta.classes;
  const held = new Set(data.meta.heldout_classes);
  return (
    <table className="w-full border-collapse">
      <thead className="border-b border-rule">
        <tr>
          <th scope="col" className={TH}>Approach</th>
          {classes.map((c) => (
            <th key={c} scope="col" className={THR}>
              {c.replace(/_/g, " ")}
              {held.has(c) && <span className="block text-meta text-late">held-out</span>}
            </th>
          ))}
        </tr>
      </thead>
      <tbody className="divide-y divide-rule/70">
        {order.map((name) => (
          <tr key={name}>
            <th scope="row" className={`${TD} text-left font-mono font-semibold`}>{name}</th>
            {classes.map((c) => {
              const x = data.per_class[name]?.[c];
              const full = x && x.n > 0 && x.correct === x.n;
              return (
                <td key={c} className={`${TDR} ${full ? "text-verified" : x && x.correct === 0 ? "text-falsified" : ""}`}>
                  {x ? `${x.correct}/${x.n}` : "n/a"}
                </td>
              );
            })}
          </tr>
        ))}
      </tbody>
    </table>
  );
}

/** FalkorDB vs Postgres p50/p95 per operation, with the results-agree count. */
export function LatencyTable({ lat, extra }: { lat: BenchLatency; extra?: "size" }) {
  const ops = Object.entries(lat.ops);
  return (
    <table className="w-full border-collapse">
      <thead className="border-b border-rule">
        <tr>
          <th scope="col" className={TH}>Operation</th>
          <th scope="col" className={THR}>FalkorDB p50</th>
          <th scope="col" className={THR}>p95</th>
          <th scope="col" className={THR}>Postgres p50</th>
          <th scope="col" className={THR}>p95</th>
          {extra === "size" && <th scope="col" className={THR}>Mean result</th>}
          <th scope="col" className={THR}>Agree</th>
        </tr>
      </thead>
      <tbody className="divide-y divide-rule/70">
        {ops.map(([key, op]) => {
          const fFaster = op.falkordb.p50_ms < op.postgres.p50_ms;
          return (
            <tr key={key} data-op={key}>
              <th scope="row" className={`${TD} text-left font-normal`}>{op.label}</th>
              <td className={`${TDR} ${fFaster ? "text-verified" : ""}`}>{ms(op.falkordb.p50_ms, 2)}</td>
              <td className={`${TDR} text-dim`}>{ms(op.falkordb.p95_ms, 2)}</td>
              <td className={`${TDR} ${!fFaster ? "text-verified" : ""}`}>{ms(op.postgres.p50_ms, 2)}</td>
              <td className={`${TDR} text-dim`}>{ms(op.postgres.p95_ms, 2)}</td>
              {extra === "size" && (
                <td className={`${TDR} text-dim`}>
                  {op.mean_size == null ? "n/a" : `${op.mean_size.toFixed(1)} ${key === "path" ? "hops" : key === "closure" ? "svcs" : ""}`}
                </td>
              )}
              <td className={`${TDR} ${op.agreed === op.checked ? "" : "text-drifted"}`}>
                {op.agreed}/{op.checked}
                {op.exact && <span className="block text-meta text-faint">exact: F {op.exact.falkordb}, P {op.exact.postgres}</span>}
              </td>
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}

const COUNT_ORDER = ["services", "depends_on", "observed_calls", "impacts_service_edges", "changes", "postmortems"];

const countRank = (k: string) => (COUNT_ORDER.includes(k) ? COUNT_ORDER.indexOf(k) : COUNT_ORDER.length);

function Counts({ counts }: { counts: Record<string, number> }) {
  return (
    <span>
      {Object.entries(counts)
        .sort(([a], [b]) => countRank(a) - countRank(b) || a.localeCompare(b))
        .map(([k, v]) => `${v.toLocaleString("en-US")} ${(v === 1 ? k.replace(/s$/, "") : k).replace(/_/g, " ")}`)
        .join(" · ")}
    </span>
  );
}

function ScaleSection({ scale }: { scale: BenchScale }) {
  return (
    <Section title="Scale: FalkorDB vs Postgres on a large world" meta={`generate_world(${scale.seed}, n_services=${scale.n_services_requested})`}>
      {scale.error ? (
        <p className="px-inset py-4 text-body text-drifted">Not measured: {scale.error}</p>
      ) : (
        <>
          <p className="px-inset pt-3 text-body text-dim">
            <Counts counts={scale.counts} />. Closure bounded at {scale.closure_depth} hops; shortest paths between the farthest reachable pairs.{" "}
            {scale.iterations} timed runs per operation and engine, warm cache, same host.
          </p>
          <LatencyTable lat={scale} extra="size" />
          {!!scale.notes?.length && (
            <ul className="space-y-1 border-t border-rule px-inset py-3 text-body text-dim">
              {scale.notes.map((n) => <li key={n}>{n}</li>)}
            </ul>
          )}
        </>
      )}
    </Section>
  );
}

export function BenchmarkTables({ data }: { data: BenchmarkResponse }) {
  const m = data.meta;
  const lat = data.latency;
  const lc = data.learning_curve;
  return (
    <div className="space-y-5">
      <Section title="Accuracy, time and tokens" meta={`${m.cases} injected faults · exact root-cause id from the answer key`}>
        <AccuracyTable data={data} />
      </Section>

      <div className="grid grid-cols-1 gap-5 xl:grid-cols-[minmax(0,1.05fr)_minmax(0,1fr)]">
        <Section title="Correct per fault class" meta={`held-out: ${m.heldout_classes.join(", ")}`}>
          <PerClassTable data={data} />
        </Section>
        <Section title="Per-step latency: FalkorDB vs Postgres" meta={lat && !lat.error ? `${lat.world}, ms` : undefined}>
          {lat && !lat.error ? (
            <>
              <LatencyTable lat={lat} />
              <p className="border-t border-rule px-inset py-2 text-label text-faint">
                <Counts counts={lat.counts} />. FalkorDB reads a materialized as-of snapshot; Postgres filters valid time inside the query.
              </p>
            </>
          ) : (
            <p className="px-inset py-4 text-body text-faint">Not measured{lat?.error ? `: ${lat.error}` : ""}.</p>
          )}
        </Section>
      </div>

      <div className="grid grid-cols-1 gap-5 xl:grid-cols-[minmax(0,1.4fr)_minmax(0,1fr)]">
        {data.scale ? (
          <ScaleSection scale={data.scale} />
        ) : (
          <Section title="Scale"><p className="px-inset py-4 text-body text-faint">Not measured yet (run `ic bench --scale N`).</p></Section>
        )}
        <Section title="Learning curve" meta={lc ? `${lc.incidents.length} incidents, one session with memory` : undefined}>
          {lc ? <LearningCurveChart data={lc} /> : <p className="px-inset py-4 text-body text-faint">Not measured yet (run `ic learn`).</p>}
        </Section>
      </div>

      {!!data.live_llm_runs?.length && (
        <Section title="Live LLM runs" meta="real language model behind each approach · separate from the headline table">
          <LiveRunsTable runs={data.live_llm_runs} />
          {data.live_llm_runs
            .filter((r) => !!r.why_wrong?.length)
            .map((r) => (
              <WhyWrongRun key={`${r.model}:${r.cases}`} run={r} />
            ))}
          <p className="border-t border-rule px-inset py-2 text-label text-faint">
            {data.live_llm_runs.map((r) => `${r.model}: ${r.cases} cases (${r.n_seen} seen, ${r.n_heldout} held-out)${r.commit ? `, commit ${r.commit.slice(0, 12)}` : ""}`).join(" · ")}
          </p>
        </Section>
      )}

      <footer className="border border-rule bg-deck px-inset py-4 text-body text-dim">
        <div className="max-w-[90ch] space-y-1.5 text-body text-ink/90">
          {honestyNotes(data).map((n) => (
            <p key={n}>{n}</p>
          ))}
        </div>
        {!!m.notes?.length && (
          <ul className="mt-3 list-disc space-y-1 pl-5">
            {m.notes.map((n) => <li key={n}>{n}</li>)}
          </ul>
        )}
        <p className="mt-3 font-mono text-label text-faint">
          seed {m.seed} · case seeds {m.case_seeds_digest ?? "n/a"}
          {m.case_seeds ? ` (${m.case_seeds.length} listed in report.json)` : ""} · {m.cases} cases ({m.n_seen} seen, {m.n_heldout} held-out) · commit{" "}
          {m.commit ? m.commit.slice(0, 12) : "unknown"}
          {m.dirty ? " (dirty tree)" : ""} · LLM: {m.llm}
          {m.generated_at ? ` · generated ${m.generated_at}` : ""}
        </p>
      </footer>
    </div>
  );
}

/** /benchmark: loads GET /api/benchmark (public, read-only; nothing here can start a run). */
export function BenchmarkView() {
  const [data, setData] = useState<BenchmarkResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    let cancelled = false;
    getBenchmark()
      .then((d) => !cancelled && setData(d))
      .catch((e) => !cancelled && setError(e instanceof Error ? e.message : "unavailable"));
    return () => {
      cancelled = true;
    };
  }, []);
  return (
    <div className="h-screen overflow-y-auto">
      <PageHeader
        sticky
        title="Benchmark"
        status={
          data && (
            <>
              <Badge tone="info">{data.meta.cases} cases</Badge>
              <Badge tone="muted">seed {data.meta.seed}</Badge>
              <Badge tone={/^none/i.test(data.meta.llm) ? "degraded" : "verified"}>{/^none/i.test(data.meta.llm) ? "NO LLM" : data.meta.llm}</Badge>
            </>
          )
        }
        right={<PageNav current="benchmark" />}
      />
      <main className="mx-auto max-w-[1600px] px-4 py-6 sm:px-8">
        {data ? (
          <BenchmarkTables data={data} />
        ) : (
          <p className="text-body text-faint" role={error ? "alert" : undefined}>
            {error ? `No benchmark report available (${error}). Run \`ic bench\` to generate one.` : "Loading the benchmark report."}
          </p>
        )}
      </main>
    </div>
  );
}
