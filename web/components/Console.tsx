"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import Link from "next/link";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { deriveBanners, isDegraded, type GraphView } from "@/lib/reducer";
import { SCENARIOS, resolveScenario, type Scenario } from "@/lib/scenarios";
import { SPEEDS, useRunStream, type StreamSource } from "@/lib/useRunStream";
import {
  FAULT_CLASSES,
  FallbackError,
  createSession,
  getIncidentGraph,
  getRecordedHeroGraph,
  getSessionIncidents,
  type IncidentGraphResponse,
  injectFault,
  startStorm,
  verifyIncident,
  type FaultClass,
} from "@/lib/api";
import { AUTOPLAY_MARKER, controlsDisabled, shouldApplyAutoplay, shouldAutoplay, sourceFor } from "@/lib/autoplay";
import { liveGraphView, pickGraph, toLiveGraph } from "@/lib/liveGraph";
import { mapVerify, type ChainVerify } from "@/lib/verify";
import { HERO_EDGES, HERO_NODES } from "@/lib/mock/hero-graph";
import { mockVerify, type VerifyMode, type VerifyResult } from "@/lib/mock/verify";
import { Badge, Button, Panel, Placeholder, Segmented, Select, buttonClass, type Tone } from "./ui";
import { GRAPH_CELL, GRAPH_PANEL, PageHeader, PageNav, RUN_LEFT, RUN_MAIN, RUN_RIGHT } from "./Shell";
import { GraphCanvas } from "./GraphCanvas";
import { CausalPathList } from "./CausalPathList";
import { Funnel } from "./Funnel";
import { Certificate } from "./Certificate";
import { HypothesisChain } from "./HypothesisChain";
import { Verdict, Response } from "./Response";
import { Reflex } from "./Reflex";
import { StateBanner } from "./StateBanner";
import { RunLog } from "./RunLog";
import { Precedent } from "./Precedent";
import { Challenge } from "./Challenge";
import { Ask } from "./Ask";
import { RecommendationBanner } from "./RecommendationBanner";
import { LearningCurve } from "./LearningCurve";
import { StormPanel } from "./Storm";

const PHASE_TONE: Record<string, Tone> = {
  DIAGNOSED: "verified", RESOLVED: "verified", LEARNED: "verified", PR_OPEN: "verified",
  INCONCLUSIVE: "drifted", REHEARSING: "info", ACTING: "info", TRIAGING: "info", OPEN: "muted",
  FAILED: "falsified", REFUSED: "falsified",
};

/** Remember in this tab that the visitor started something, so autoplay never replays over it. */
function markStarted() {
  try { window.sessionStorage.setItem(AUTOPLAY_MARKER, "1"); } catch { /* storage blocked: fine */ }
}

/** A started live run, or the recorded fallback when the backend refused. */
export type LiveSession = { runId: string; incidentId: string } | { recorded: true; reason?: string };

export default function Console() {
  const params = useSearchParams();
  const scenarioParam = params.get("scenario");
  const scenario = scenarioParam === null ? null : resolveScenario(scenarioParam);
  // An explicit ?verify= forces the mocked verifier even against a live run.
  const verifyParam = params.get("verify") as VerifyMode | null;
  const [session, setSession] = useState<LiveSession | null>(null);
  const [gen, setGen] = useState(0);

  // First paint: play the recorded hero under its "Recorded run" label until the visitor starts
  // something. sessionStorage is touched only in effects/handlers so SSR stays safe.
  const sessionRef = useRef<LiveSession | null>(null);
  useEffect(() => { sessionRef.current = session; }, [session]);
  // Set synchronously by the first click, before any await, so a pending autoplay check can't remount over it.
  const userActedRef = useRef(false);
  const onUserAct = useCallback(() => {
    userActedRef.current = true;
    markStarted();
  }, []);
  useEffect(() => {
    let cancelled = false;
    const marker = (() => {
      try { return window.sessionStorage.getItem(AUTOPLAY_MARKER) !== null; } catch { return false; }
    })();
    if (!shouldAutoplay({ hasLiveSession: false, scenarioParam, marker, sessionHasIncident: false })) return;
    // A session that already lists an incident (another tab, a returning visitor) is never played over.
    getSessionIncidents()
      .catch(() => [] as string[])
      .then((ids) => {
        if (cancelled || userActedRef.current || sessionRef.current) return;
        const stillMarked = (() => {
          try { return window.sessionStorage.getItem(AUTOPLAY_MARKER) !== null; } catch { return false; }
        })();
        if (!shouldApplyAutoplay({ cancelled, userActed: userActedRef.current, hasLiveSession: false, scenarioParam, marker: stillMarked, sessionHasIncident: ids.length > 0 })) return;
        setSession({ recorded: true });
        setGen((g) => g + 1);
      });
    return () => { cancelled = true; };
    // Runs once per scenario param; later session changes come from user actions.
  }, [scenarioParam]);

  // Any user-started action takes over from autoplay and remembers it for this tab.
  const start = (x: LiveSession) => {
    onUserAct();
    setSession(x);
    setGen((g) => g + 1);
  };
  // Keyed so switching scenario / starting a run resets stream, verification and focus.
  return (
    <RunConsole
      key={`${scenario?.id ?? "live"}:${gen}`}
      scenario={scenario}
      verifyParam={verifyParam}
      session={session}
      onSession={start}
      onUserAct={onUserAct}
    />
  );
}

type Tab = "log" | "precedent" | "challenge" | "ask";

function RunConsole({
  scenario,
  verifyParam,
  session,
  onSession,
  onUserAct,
}: {
  scenario: Scenario | null;
  verifyParam: VerifyMode | null;
  session: LiveSession | null;
  onSession: (s: LiveSession) => void;
  /** Called synchronously on a click of Inject/Storm, before any await. */
  onUserAct: () => void;
}) {
  const router = useRouter();
  const pathname = usePathname();
  const liveSession = scenario ? null : session;
  const source: StreamSource = sourceFor(scenario, liveSession);
  const stream = useRunStream(source, { autoplay: true });
  const verifyMode: VerifyMode = verifyParam ?? "ok";
  const incidentId = liveSession && "runId" in liveSession ? liveSession.incidentId : null;
  const useLiveVerify = incidentId !== null && verifyParam === null && stream.kind === "live";
  const replayable = stream.kind !== "live";
  const [fault, setFault] = useState<FaultClass>("bad_deploy");
  const [injecting, setInjecting] = useState(false);
  const locked = controlsDisabled({ scenario: !!scenario, injecting, streamActive: stream.active, streamRecorded: stream.kind === "recorded" });
  const [injectError, setInjectError] = useState<string | null>(null);
  const [storm, setStorm] = useState<{ runId: string } | null>(null);
  const [storming, setStorming] = useState(false);
  const [chain, setChain] = useState<ChainVerify | null>(null);
  const { state: s } = stream;
  const [view, setView] = useState<"true" | "known">("true");
  const [drawer, setDrawer] = useState<Tab | null>(null);
  const [focus, setFocus] = useState<{ ids: string[]; n: number } | null>(null);
  const [results, setResults] = useState<Record<string, VerifyResult>>({});
  const [running, setRunning] = useState<Record<string, boolean>>({});
  const timers = useRef<ReturnType<typeof setTimeout>[]>([]);
  useEffect(() => {
    const t = timers.current;
    return () => t.forEach(clearTimeout);
  }, []);

  const doFocus = useCallback((ids: string[]) => setFocus({ ids, n: Date.now() }), []);

  const inject = useCallback(async () => {
    onUserAct();
    setInjecting(true);
    setInjectError(null);
    try {
      await createSession();
      const r = await injectFault(fault);
      onSession({ runId: r.run_id, incidentId: r.incident_id });
    } catch (e) {
      if (e instanceof FallbackError) {
        onSession({ recorded: true, reason: e.reason });
      } else {
        setInjectError(e instanceof Error ? e.message : "inject failed");
        setInjecting(false);
      }
    }
  }, [fault, onSession, onUserAct]);

  const fireStorm = useCallback(async () => {
    onUserAct();
    setStorming(true);
    setInjectError(null);
    try {
      await createSession();
      const r = await startStorm();
      setStorm({ runId: r.run_id });
    } catch (e) {
      setInjectError(e instanceof FallbackError ? `Storm unavailable (${e.reason ?? "backend at its cap"})` : e instanceof Error ? e.message : "storm failed");
    } finally {
      setStorming(false);
    }
  }, [onUserAct]);

  const verifyLive = useCallback(
    async (ids: string[]) => {
      if (!incidentId) return;
      ids.forEach((id) => setRunning((r) => ({ ...r, [id]: true })));
      try {
        const resp = await verifyIncident(incidentId);
        const mapped = mapVerify(resp, s.proofs);
        setResults((r) => ({ ...r, ...mapped.byHypothesis }));
        setChain(mapped.chain);
        const bad = Object.values(mapped.byHypothesis).find((x) => x.state !== "VERIFIED" && x.node);
        if (bad?.node) doFocus([bad.node]);
      } catch (e) {
        const detail = e instanceof Error ? e.message : "verify failed";
        setChain({ chain_ok: false, anchor_ok: false, result: { state: "TAMPERED", detail, recomputed: "", reasons: [`Verify call failed: ${detail}`] } });
      } finally {
        ids.forEach((id) => setRunning((r) => ({ ...r, [id]: false })));
      }
    },
    [incidentId, s.proofs, doFocus],
  );

  const verify = useCallback(
    (ids: string[]) => {
      if (useLiveVerify) {
        void verifyLive(ids);
        return;
      }
      ids.forEach((id, i) => {
        setRunning((r) => ({ ...r, [id]: true }));
        timers.current.push(
          setTimeout(() => {
            const p = s.proofs.find((x) => x.hypothesis_id === id);
            if (p) {
              const res = mockVerify(p, verifyMode, p === s.proofs[s.proofs.length - 1]);
              setResults((r) => ({ ...r, [id]: res }));
              if (res.state !== "VERIFIED" && res.node) doFocus([res.node]);
            }
            setRunning((r) => ({ ...r, [id]: false }));
          }, 500 + i * 350),
        );
      });
    },
    [s.proofs, verifyMode, doFocus, useLiveVerify, verifyLive],
  );

  // Live runs draw the real incident graph, recorded playback the graph saved with the recording
  // (fetched once; the mock hero graph if that fails); scripted scenarios keep the mock hero graph.
  const liveGraphId = stream.kind === "live" ? (s.incident?.incident_id ?? null) : null;
  const hasSnapshot = !!s.snapshot;
  const [fetched, setFetched] = useState<{ id: string; resp: IncidentGraphResponse | null; error: string | null } | null>(null);
  useEffect(() => {
    if (!liveGraphId) return;
    let cancelled = false;
    getIncidentGraph(liveGraphId)
      .then((resp) => !cancelled && setFetched({ id: liveGraphId, resp, error: null }))
      .catch((e) => !cancelled && setFetched((f) => (f?.id === liveGraphId && f.resp ? f : { id: liveGraphId, resp: null, error: e instanceof Error ? e.message : "graph fetch failed" })));
    return () => {
      cancelled = true;
    };
  }, [liveGraphId, hasSnapshot]);
  const isRecorded = stream.kind === "recorded";
  const [recordedGraph, setRecordedGraph] = useState<IncidentGraphResponse | null | undefined>(undefined);
  useEffect(() => {
    if (!isRecorded || recordedGraph !== undefined) return;
    let cancelled = false;
    getRecordedHeroGraph()
      .then((resp) => !cancelled && setRecordedGraph(resp))
      .catch(() => !cancelled && setRecordedGraph(null));
    return () => {
      cancelled = true;
    };
  }, [isRecorded, recordedGraph]);
  const fetchedResp = fetched && fetched.id === liveGraphId ? fetched.resp : null;
  const graphError = fetched && fetched.id === liveGraphId ? fetched.error : null;
  const { resp: liveResp, pending: recordedGraphPending } = pickGraph({
    kind: stream.kind,
    incidentId: s.incident?.incident_id ?? null,
    live: fetchedResp,
    recorded: recordedGraph,
  });
  const live = useMemo(() => (liveResp ? toLiveGraph(liveResp) : null), [liveResp]);
  // Ask the graph's source chips focus a node only when the canvas is showing it.
  const graphIds = useMemo(() => new Set(s.incident ? (live?.nodes ?? HERO_NODES).map((n) => n.id) : []), [s.incident, live]);
  const inGraph = useCallback((id: string) => graphIds.has(id), [graphIds]);
  const falsified = useMemo(
    () => Object.values(results).filter((r) => r.state === "FALSIFIED" && r.node).map((r) => r.node as string),
    [results],
  );
  const graph: GraphView = useMemo(() => {
    const base = liveResp ? liveGraphView(liveResp, s) : s.graph;
    return { ...base, falsified };
  }, [liveResp, s, falsified]);
  const banners = deriveBanners(s);
  if (stream.kind === "recorded" && !banners.some((b) => b.kind === "recorded")) {
    banners.unshift({
      kind: "recorded",
      tone: "info",
      title: "Recorded run",
      body: stream.error
        ? `Live backend unavailable and the recorded run could not be loaded: ${stream.error}`
        : "The live backend is unavailable or at its cap, so a saved run is playing. Queries are real; nothing is executing against a live graph.",
    });
  }
  if (stream.kind === "live" && stream.status === "reconnecting") {
    banners.push({ kind: "action_error", tone: "warn", title: "Reconnecting", body: "Event stream dropped; resuming from the last received event." });
  }
  if (stream.kind === "live" && stream.status === "error") {
    banners.push({ kind: "failed", tone: "bad", title: "Stream lost", body: stream.error ?? "The event stream could not be re-established." });
  }
  const phase = s.phase;
  const outcome = s.verdict?.outcome;

  const setScenario = (id: string) => {
    markStarted();
    router.replace(id === "live" ? pathname : `${pathname}?scenario=${id}`);
  };

  return (
    <div className="flex h-screen flex-col overflow-y-auto lg:grid lg:min-h-[640px] lg:grid-rows-[auto_auto_minmax(0,1fr)_auto] lg:overflow-hidden">
      <PageHeader
        title={s.incident?.title ?? "No incident"}
        status={
          <>
            {phase && <Badge tone={PHASE_TONE[phase] ?? "muted"} title="Incident state">{phase}</Badge>}
            {outcome && <Badge tone={outcome === "DIAGNOSED" ? "verified" : outcome === "INCONCLUSIVE" ? "drifted" : "falsified"} title="Verdict">VERDICT {outcome}</Badge>}
            {isDegraded(s) && <Badge tone="degraded">DEGRADED</Badge>}
          </>
        }
        right={
          <>
            {stream.kind === "live" && (
              <span className="font-mono text-meta tabular-nums text-dim" aria-live="polite">
                {stream.status === "idle" ? "idle" : `${stream.status} · ${stream.total} events`}
              </span>
            )}
            {replayable && (
              <div className="flex items-center gap-2" role="group" aria-label="Playback">
                <Button size="sm" onClick={stream.playing ? stream.pause : stream.play} className="w-14">
                  {stream.playing ? "Pause" : stream.cursor >= stream.total ? "Replay" : "Play"}
                </Button>
                <label className="sr-only" htmlFor="scrub">Position in event stream</label>
                <input
                  id="scrub"
                  type="range"
                  min={0}
                  max={stream.total}
                  value={stream.cursor}
                  onChange={(e) => { stream.pause(); stream.seek(Number(e.target.value)); }}
                  className="w-24 accent-symptom"
                />
                <span className="w-11 font-mono text-meta tabular-nums text-dim">{stream.cursor}/{stream.total}</span>
                <div className="flex h-6 overflow-hidden rounded-ctl border border-rule-hi" role="radiogroup" aria-label="Playback speed">
                  {SPEEDS.map((sp) => (
                    <button
                      key={sp}
                      type="button"
                      role="radio"
                      aria-checked={stream.speed === sp}
                      onClick={() => stream.setSpeed(sp)}
                      className={`px-1.5 font-mono text-meta ${stream.speed === sp ? "bg-symptom/15 text-ink" : "text-dim hover:text-ink"}`}
                    >
                      {sp}x
                    </button>
                  ))}
                </div>
              </div>
            )}
          </>
        }
      >
        <div className="flex flex-wrap items-center gap-x-4 gap-y-2 border-t border-rule/60 px-4 py-2 sm:px-5">
          <div className="flex flex-wrap items-center gap-2" role="group" aria-label="Inject fault">
            <label className="sr-only" htmlFor="fault-class">Fault class</label>
            <Select id="fault-class" value={fault} onChange={(e) => setFault(e.target.value as FaultClass)} disabled={locked}>
              {FAULT_CLASSES.map((f) => <option key={f} value={f}>{f}</option>)}
            </Select>
            <Button
              variant="primary"
              onClick={() => void inject()}
              disabled={locked}
              title={scenario ? "Scenario playback is on. Choose live in the scenario list to inject." : undefined}
            >
              {injecting ? "Injecting" : "Inject fault"}
            </Button>
            <Button
              onClick={() => void fireStorm()}
              disabled={locked || storming}
              title="Fire an alert storm: 2-3 independent faults, hundreds of pages, collapsed into incidents"
            >
              {storming ? "Firing" : "Storm"}
            </Button>
            {injectError && <span role="alert" className="text-label text-falsified">{injectError}</span>}
          </div>

          <span className="hidden h-5 w-px bg-rule sm:block" aria-hidden />

          <label className="flex items-center gap-2 text-meta text-dim">
            Scenario
            <Select value={scenario?.id ?? "live"} onChange={(e) => setScenario(e.target.value)} className="max-w-60">
              <option value="live">live: inject a fault</option>
              {SCENARIOS.map((x) => <option key={x.id} value={x.id}>{x.id}: {x.label}</option>)}
            </Select>
          </label>

          <div className="flex items-center gap-2">
            <span className="text-meta text-dim" aria-hidden>Evidence</span>
            <Segmented
              name="view"
              legend="Evidence view"
              value={view}
              onChange={setView}
              options={[{ value: "known", label: "as-known" }, { value: "true", label: "as-true" }]}
            />
          </div>

          <div className="ml-auto flex items-center gap-1">
            {incidentId && s.done && stream.kind === "live" && (
              <Link href={`/replay/${encodeURIComponent(incidentId)}`} className={buttonClass("secondary")}>
                Replay
              </Link>
            )}
            <PageNav current="console" />
          </div>
        </div>
      </PageHeader>

      {storm ? (
        <StormPanel key={storm.runId} runId={storm.runId} onExit={() => setStorm(null)} />
      ) : (
      <>
      <StateBanner banners={banners} />

      <main className={RUN_MAIN}>
        <div className={RUN_LEFT}>
          <Reflex reflex={s.reflex} />
          <Panel aria-label="Graph" className={GRAPH_PANEL}>
            <div className="flex min-h-0 min-w-0 flex-col">
              {/* Narration sits above the canvas, never over it, so it cannot hide node labels. */}
              {s.narration.length > 0 && (
                <p className="shrink-0 border-b border-rule/60 px-3 py-1.5 text-label text-dim" aria-live="polite" title={s.narration[s.narration.length - 1]}>
                  <span className="line-clamp-2 sm:line-clamp-1">{s.narration[s.narration.length - 1]}</span>
                </p>
              )}
              <div className={GRAPH_CELL}>
              {graphError && !live && (
                <p role="status" className="absolute right-3 top-2.5 z-[1] rounded-ctl bg-drifted/10 px-2 py-0.5 text-meta text-drifted">
                  Graph unavailable ({graphError}). The causal path list still works.
                </p>
              )}
              {s.incident && ((liveGraphId && !live) || recordedGraphPending) ? (
                <Placeholder>{graphError ? "The service graph could not be loaded." : "Loading the service graph at alert time."}</Placeholder>
              ) : s.incident ? (
                <GraphCanvas nodes={live?.nodes ?? HERO_NODES} edges={live?.edges ?? HERO_EDGES} graph={graph} view={view} focus={focus} onNodeClick={(id) => doFocus([id])} settled={s.done || !!s.verdict} />
              ) : (
                <Placeholder>The service graph at alert time appears here once an incident starts.</Placeholder>
              )}
              </div>
            </div>
            <CausalPathList graph={graph} onFocus={doFocus} />
          </Panel>
          <Panel className="shrink-0">
            <Funnel steps={s.funnel} onFocus={doFocus} incidentId={useLiveVerify ? incidentId : null} />
          </Panel>
        </div>

        <div className={RUN_RIGHT}>
          <Verdict s={s} view={view} />
          <RecommendationBanner recs={s.recommendations} />
          <Certificate cert={s.certificate} view={view} onFocus={doFocus} />
          <div className="border-t border-rule">
            <HypothesisChain
              proofs={s.proofs}
              results={results}
              running={running}
              anchored={s.done || !!s.verdict?.chain_head}
              chain={chain}
              onVerify={(id) => verify([id])}
              onVerifyAll={() => verify(s.proofs.map((p) => p.hypothesis_id))}
              onFocusNode={(n) => doFocus([n])}
              incidentId={useLiveVerify ? incidentId : null}
            />
          </div>
          <div className="border-t border-rule"><Response s={s} /></div>
          <div className="border-t border-rule"><LearningCurve /></div>
        </div>
      </main>

      <section aria-label="Drawer" className="border-t border-rule-hi bg-deck">
        <div className="flex items-center gap-1 overflow-x-auto px-3" role="tablist" aria-label="Details">
          {([
            ["log", `Run log (${s.toolCalls.length})`],
            ["precedent", `Precedent (${s.precedent.reduce((a, p) => a + p.matches.length, 0)})`],
            ["challenge", "Was it my deploy?"],
            ["ask", "Ask the graph"],
          ] as [Tab, string][]).map(([id, label]) => (
            <button
              key={id}
              type="button"
              role="tab"
              id={`tab-${id}`}
              aria-selected={drawer === id}
              aria-controls="drawer-body"
              onClick={() => setDrawer(drawer === id ? null : id)}
              className={`-mb-px h-9 shrink-0 whitespace-nowrap border-b-2 px-3 text-label font-medium ${drawer === id ? "border-symptom text-ink" : "border-transparent text-dim hover:text-ink"}`}
            >
              {label}
            </button>
          ))}
          <span className="ml-auto hidden shrink-0 text-meta text-faint sm:inline">{drawer ? "Select the open tab to collapse" : "Collapsed"}</span>
        </div>
        {drawer && (
          <div id="drawer-body" role="tabpanel" aria-labelledby={`tab-${drawer}`} className={`${drawer === "ask" ? "h-[46vh] min-h-[280px]" : "h-[28vh] min-h-[180px]"} overflow-y-auto border-t border-rule`}>
            {drawer === "log" && <RunLog calls={s.toolCalls} t0={s.startedTs} />}
            {drawer === "precedent" && <Precedent entries={s.precedent} />}
            {drawer === "challenge" && <Challenge cert={s.certificate} verdict={s.verdict} incidentId={useLiveVerify ? incidentId : null} />}
            {drawer === "ask" && <Ask incidentId={useLiveVerify ? incidentId : null} inGraph={inGraph} onFocus={doFocus} />}
          </div>
        )}
      </section>
      </>
      )}
    </div>
  );
}
