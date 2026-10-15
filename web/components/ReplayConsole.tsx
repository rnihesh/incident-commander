"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { getIncidentReplay } from "@/lib/api";
import { toLiveGraph } from "@/lib/liveGraph";
import {
  clampIndex,
  fmtOffset,
  frameGraphView,
  landedChanges,
  nearestFrame,
  type ReplayResponse,
  type ReplaySpeed,
  type ReplayViewName,
} from "@/lib/replay";
import { Badge, Placeholder, Segmented } from "./ui";
import { PageHeader, PageNav } from "./Shell";
import { GraphCanvas } from "./GraphCanvas";
import { Scrubber } from "./Scrubber";

type Loaded = Partial<Record<ReplayViewName, ReplayResponse>>;

export default function ReplayConsole({ incidentId }: { incidentId: string }) {
  const [view, setView] = useState<ReplayViewName>("true");
  const [data, setData] = useState<Loaded>({});
  const [error, setError] = useState<string | null>(null);
  const [index, setIndex] = useState(0);
  const [playing, setPlaying] = useState(false);
  const [speed, setSpeed] = useState<ReplaySpeed>(1);

  const resp = data[view];
  const needed = !resp;
  useEffect(() => {
    if (!needed) return;
    let cancelled = false;
    getIncidentReplay(incidentId, view)
      .then((r) => !cancelled && (setData((d) => ({ ...d, [view]: r })), setError(null)))
      .catch((e) => !cancelled && setError(e instanceof Error ? e.message : "replay failed"));
    return () => {
      cancelled = true;
    };
  }, [incidentId, view, needed]);

  // Keep the same moment when toggling views (frame grids differ only if late records move the range).
  const changeView = useCallback(
    (v: ReplayViewName) => {
      const cur = data[view];
      const t = cur?.frames[clampIndex(index, cur.frames.length)]?.t;
      const target = data[v];
      if (target && t !== undefined) setIndex(nearestFrame(target.frames, t));
      setView(v);
    },
    [data, view, index],
  );
  const live = useMemo(() => (resp ? toLiveGraph(resp) : null), [resp]);
  const frame = resp ? resp.frames[clampIndex(index, resp.frames.length)] : null;
  const graph = useMemo(() => (resp && frame ? frameGraphView(resp, frame) : null), [resp, frame]);
  const landed = useMemo(() => (resp && frame ? landedChanges(resp, frame) : []), [resp, frame]);
  const nodeName = useMemo(() => new Map((resp?.nodes ?? []).map((n) => [n.id, n.name])), [resp]);
  const onsets = useMemo(
    () =>
      (resp?.nodes ?? [])
        .filter((n) => n.onset !== null)
        .map((n) => ({ id: n.id, name: n.name, onset: n.onset as number }))
        .sort((a, b) => a.onset - b.onset || a.id.localeCompare(b.id)),
    [resp],
  );
  const lateCount = resp?.changes.filter((c) => c.late).length ?? 0;

  return (
    <div className="grid h-screen min-h-[560px] grid-rows-[auto_minmax(0,1fr)_auto] overflow-hidden">
      <PageHeader
        title={<>Replay <span className="font-mono">{incidentId}</span></>}
        right={
          <>
            <Segmented
              name="replay-view"
              legend="Replay view"
              value={view}
              onChange={changeView}
              options={[{ value: "true", label: "as-happened" }, { value: "known", label: "as-known" }]}
            />
            <PageNav />
          </>
        }
      />

      {/* Below lg the timeline goes under the graph; the scrubber stays pinned at the bottom. */}
      <main className="grid min-h-0 grid-rows-[minmax(0,1.5fr)_minmax(0,1fr)] lg:grid-cols-[minmax(0,1fr)_clamp(300px,26vw,400px)] lg:grid-rows-1">
        <div className="relative min-h-0 border-b border-rule bg-deck lg:border-b-0 lg:border-r">
          {error && !resp && (
            <p role="alert" className="absolute left-3 top-3 z-[1] rounded-ctl bg-falsified/10 px-2 py-1 text-label text-falsified">
              Replay unavailable ({error}).
            </p>
          )}
          {!resp && !error && <Placeholder>Loading the replay.</Placeholder>}
          {resp && live && graph && <GraphCanvas nodes={live.nodes} edges={live.edges} graph={graph} view="true" focus={null} />}
        </div>

        <aside aria-label="Replay timeline, text" className="flex min-h-0 flex-col gap-5 overflow-y-auto bg-raise/40 px-inset py-3 text-label">
          {view === "known" && (
            <p className="text-dim">As-known: records that reached the graph only after the alert are left out.</p>
          )}
          {view === "true" && lateCount > 0 && (
            <p className="text-late">{lateCount} late-arriving record{lateCount > 1 ? "s" : ""} shown here that the agent did not have.</p>
          )}
          <section aria-label="Changes landed">
            <h2 className="text-body font-semibold">Changes landed ({landed.length})</h2>
            {landed.length === 0 ? (
              <p className="mt-1 text-faint">None yet.</p>
            ) : (
              <ul className="mt-1 space-y-1">
                {landed.map((c) => (
                  <li key={c.id} className="flex flex-wrap items-center gap-x-2 font-mono">
                    <span className="text-drifted">{c.kind}</span>
                    <span className="text-ink">{nodeName.get(c.service_id) ?? c.service_id}</span>
                    <span className="text-faint">{frame ? fmtOffset(c.valid_from, resp?.markers.alert_t ?? 0) : ""}</span>
                    {c.badge && <Badge tone="tampered" title="Recorded after the alert; the agent could not see it">LATE-ARRIVING</Badge>}
                  </li>
                ))}
              </ul>
            )}
          </section>
          <section aria-label="Services turning symptomatic">
            <h2 className="text-body font-semibold">Symptomatic ({graph?.symptomatic.length ?? 0}/{onsets.length})</h2>
            <ol className="mt-1 space-y-0.5 font-mono">
              {onsets.map((o) => {
                const on = !!graph?.symptomatic.includes(o.id);
                return (
                  <li key={o.id} className={on ? "text-symptom" : "text-faint"}>
                    {o.name} <span className="text-meta">{resp ? fmtOffset(o.onset, resp.markers.alert_t) : ""}</span>
                    {on && <span className="sr-only"> symptomatic</span>}
                  </li>
                );
              })}
            </ol>
          </section>
        </aside>
      </main>

      {resp ? (
        <Scrubber
          frames={resp.frames}
          markers={resp.markers}
          index={clampIndex(index, resp.frames.length)}
          onIndex={setIndex}
          playing={playing}
          onPlayingChange={setPlaying}
          speed={speed}
          onSpeed={setSpeed}
        />
      ) : (
        <div className="border-t border-rule-hi bg-deck px-5 py-4 text-meta text-faint">Controls appear once the replay loads.</div>
      )}
    </div>
  );
}
