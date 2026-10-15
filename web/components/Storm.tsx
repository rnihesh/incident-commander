"use client";

import { useEffect, useMemo, useState } from "react";
import { streamRunEvents } from "../lib/sse";
import { counter, initialStorm, reduceStorm, stormCanvas, type StormIncident, type StormState } from "../lib/storm";
import { Badge, Button, Cypher, Empty, PanelHead, Placeholder, shortSvc, shortSvcs } from "./ui";
import { GraphCanvas } from "./GraphCanvas";
import { RUN_LEFT, RUN_MAIN, RUN_RIGHT } from "./Shell";

const hhmm = (t: number) => new Date(t * 1000).toISOString().slice(11, 19);
const mins = (s: number) => `${Math.round(s / 60)} min`;

/** The page counter and intake strip: pages arriving in batches, then pages -> incidents. */
export function IntakeStrip({ s }: { s: StormState }) {
  const { pages, incidents } = counter(s);
  const peak = Math.max(1, ...s.batches);
  return (
    <section aria-label="Alert intake" className="shrink-0 border border-rule bg-deck px-inset py-3">
      <div className="flex flex-wrap items-end gap-x-6 gap-y-2">
        <p className="flex items-baseline gap-3 font-mono tabular-nums" aria-live="polite">
          <span className={`text-hero font-semibold leading-none ${incidents === null ? "text-falsified" : "text-faint line-through decoration-1"}`}>{pages}</span>
          <span className="text-body text-dim">pages</span>
          {incidents !== null && (
            <>
              <span className="text-display text-faint" aria-hidden>{"->"}</span>
              <span className="text-hero font-semibold leading-none text-verified">{incidents}</span>
              <span className="text-body text-dim">{incidents === 1 ? "incident" : "incidents"}</span>
            </>
          )}
        </p>
        <div className="flex h-10 items-end gap-1" aria-hidden>
          {s.batches.map((n, i) => (
            <span key={i} className="arrive w-3 bg-falsified/60" style={{ height: `${Math.max(8, (n / peak) * 100)}%` }} />
          ))}
        </div>
        <dl className="ml-auto grid grid-cols-[auto_auto] gap-x-3 gap-y-0.5 font-mono text-meta text-dim">
          <dt>received</dt>
          <dd className="text-right tabular-nums text-ink">{s.received}/{s.total || "?"}</dd>
          <dt>services paging</dt>
          <dd className="text-right tabular-nums text-ink">{s.servicesPaging}</dd>
          {s.grouped && (
            <>
              <dt>changes checked</dt>
              <dd className="text-right tabular-nums text-ink">{s.grouped.candidates_checked}</dd>
            </>
          )}
        </dl>
      </div>
    </section>
  );
}

/** One collapsed incident: its pages, services and the minimal explaining change set. */
export function StormCard({ incident, selected = false, onSelect }: { incident: StormIncident; selected?: boolean; onSelect?: (n: number) => void }) {
  const i = incident;
  const joint = i.jointly_caused && i.joint;
  const names = shortSvcs(i.symptomatic);
  const tone = joint ? "border-tampered/70" : i.explaining.length ? "border-verified/50" : "border-drifted/60";
  return (
    <li className="arrive">
      <button
        type="button"
        aria-pressed={selected}
        onClick={() => onSelect?.(i.component)}
        className={`block w-full border-l-[3px] ${tone} border-y border-r border-y-rule border-r-rule px-3 py-2.5 text-left hover:bg-raise ${selected ? "bg-raise" : "bg-deck"}`}
      >
        <div className="flex items-center justify-between gap-3">
          <span className="font-mono text-label text-ink">incident {i.component}</span>
          <span className="flex items-center gap-2">
            {joint && <Badge tone="tampered">jointly caused</Badge>}
            <span className="font-mono text-meta tabular-nums text-dim">{i.alerts} pages · {i.symptomatic.length} services</span>
          </span>
        </div>
        <p className="mt-1 truncate text-meta text-dim" title={i.symptomatic.join(", ")}>
          {names.join(", ")} · first page {hhmm(i.first_page)}
        </p>
        {joint ? (
          <div className="mt-2 border border-tampered/40 bg-tampered/5 px-2 py-1.5">
            <p className="font-mono text-label text-ink">
              jointly caused: <span className="text-tampered">{joint.flag_change}</span>{" "}
              <span className="rounded-[2px] border border-tampered/60 px-1 text-meta text-tampered">AND</span>{" "}
              <span className="text-tampered">{joint.config_change}</span>
            </p>
            <ul className="mt-1 space-y-0.5 text-meta text-dim">
              {i.explaining.map((c) => {
                const solo = joint.solo[c.id];
                return (
                  <li key={c.id}>
                    <span className="font-mono text-ink/90">{c.kind}</span> {c.summary ?? c.id} on {shortSvc(c.service_id)}
                    {solo && <span className="text-faint"> · ran alone {mins(solo.seconds)} with no symptom (outage {mins(solo.lead_s)} after both)</span>}
                  </li>
                );
              })}
            </ul>
          </div>
        ) : i.explaining.length ? (
          <ul className="mt-2 space-y-1">
            {i.explaining.map((c) => (
              <li key={c.id} className="flex items-baseline gap-2 text-label">
                <span className="font-mono text-verified">{c.id}</span>
                <span className="font-mono text-meta text-dim">{c.kind}</span>
                <span className="truncate text-ink/90">{c.summary ?? ""}</span>
                <span className="ml-auto shrink-0 font-mono text-meta text-faint">{shortSvc(c.service_id)} {hhmm(c.event_time)}</span>
              </li>
            ))}
          </ul>
        ) : (
          <p className="mt-2 text-label text-drifted">No change in the window explains these pages.</p>
        )}
        {i.unexplained.length > 0 && i.explaining.length > 0 && (
          <p className="mt-1 text-meta text-drifted">Unexplained: {shortSvcs(i.unexplained).join(", ")}</p>
        )}
        {i.alternatives.length > 0 && (
          <p className="mt-1 text-meta text-drifted">Equally explained by {i.alternatives.map((a) => a.join(" + ")).join(", ")}</p>
        )}
      </button>
    </li>
  );
}

export function StormPanel({ runId, onExit }: { runId: string; onExit: () => void }) {
  const [s, setS] = useState<StormState>(initialStorm);
  const [status, setStatus] = useState("connecting");
  const [focus, setFocus] = useState<number | null>(null);

  useEffect(() => {
    const ctrl = new AbortController();
    streamRunEvents(runId, {
      signal: ctrl.signal,
      onStatus: setStatus,
      onEvent: (ev) => setS((x) => reduceStorm(x, ev)),
    }).then(
      () => { if (!ctrl.signal.aborted) setStatus("closed"); },
      (e: unknown) => { if (!ctrl.signal.aborted) setStatus(e instanceof Error ? `error: ${e.message}` : "error"); },
    );
    return () => ctrl.abort();
  }, [runId]);

  const canvas = useMemo(() => stormCanvas(s, focus), [s, focus]);
  // Selecting a card filters the canvas to that incident; the canvas re-fits on its own.
  const select = (n: number) => setFocus(focus === n ? null : n);
  const tool = s.tool;
  const wcc = tool ? tool.cypher.findIndex((q) => q.includes("algo.WCC")) : -1;
  const btw = tool ? tool.cypher.findIndex((q) => q.includes("algo.betweenness")) : -1;

  return (
    <main className={`${RUN_MAIN} lg:row-span-3`}>
      <div className={RUN_LEFT}>
        <IntakeStrip s={s} />
        <section aria-label="Storm area" className="relative h-[min(60vh,440px)] min-h-[300px] shrink-0 border border-rule bg-deck lg:h-auto lg:min-h-[380px] lg:flex-1">
          {canvas.nodes.length ? (
            <div className="absolute inset-0">
              <GraphCanvas nodes={canvas.nodes} edges={canvas.edges} graph={canvas.graph} view="true" focus={null} settled={s.done} />
            </div>
          ) : (
            <Placeholder>{s.error ? s.error : "The storm area appears once the pages are grouped."}</Placeholder>
          )}
          {focus !== null && (
            <Button size="sm" onClick={() => select(focus)} className="absolute right-3 top-2.5 z-[1]">
              Showing incident {focus} · show all
            </Button>
          )}
        </section>
      </div>
      <div className={RUN_RIGHT}>
        <PanelHead
          title="Storm collapse"
          meta={`${s.stormId ?? "starting"} · ${s.done ? "done" : status}`}
          right={
            <Button size="sm" onClick={onExit}>
              Exit storm
            </Button>
          }
        />
        {s.error && <p role="alert" className="px-inset py-2 text-label text-falsified">{s.error}</p>}
        {!s.incidents.length ? (
          <Empty>Pages are grouped by blast area and explaining change (algo.WCC); each group becomes one incident card.</Empty>
        ) : (
          <ol className="space-y-2 px-3 py-3" aria-label="Incidents">
            {s.incidents.map((i) => (
              <StormCard key={i.component} incident={i} selected={focus === i.component} onSelect={select} />
            ))}
          </ol>
        )}
        <PanelHead title="Chokepoints" meta={s.chokepoints.length ? "seeded algo.betweenness, storm area" : undefined} />
        {s.chokepoints.length ? (
          <ol className="px-inset py-2 font-mono text-label">
            {s.chokepoints.map((c) => (
              <li key={c.service_id} className="flex justify-between border-b border-rule/60 py-1">
                <span className="text-ink">{shortSvc(c.service_id)} <span className="text-faint">{c.service_id.split("@")[1]}</span></span>
                <span className="tabular-nums text-tampered">{c.score.toFixed(1)}</span>
              </li>
            ))}
          </ol>
        ) : (
          <Empty>Ranked once the incidents are explained.</Empty>
        )}
        {tool && (
          <details className="border-t border-rule px-inset py-2">
            <summary className="cursor-pointer font-mono text-meta text-dim hover:text-ink">
              collapse_storm ran {tool.cypher.length} queries in {Math.round(tool.ms)} ms
            </summary>
            <div className="mt-2 space-y-2">
              {wcc >= 0 && <Cypher text={tool.cypher[wcc]} params={tool.params[wcc]} />}
              {btw >= 0 && <Cypher text={tool.cypher[btw]} params={tool.params[btw]} />}
            </div>
          </details>
        )}
      </div>
    </main>
  );
}
