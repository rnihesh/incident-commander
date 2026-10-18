"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { deriveBanners, isDegraded, type GraphView } from "@/lib/reducer";
import { useRunStream } from "@/lib/useRunStream";
import { getIncidentGraph, verifyIncident, type IncidentGraphResponse, type VerifyResponse } from "@/lib/api";
import { fitNodes, liveGraphView, toLiveGraph } from "@/lib/liveGraph";
import { mapVerify, type ChainVerify } from "@/lib/verify";
import type { VerifyResult } from "@/lib/mock/verify";
import {
  getServiceChanges,
  getTargets,
  judgeInject,
  recomputeCommitment,
  rederive,
  revealFault,
  tamper,
  type JudgeFaultClass,
  type RevealResponse,
  type ServiceChanges,
  type TamperAction,
  type TamperResponse,
  type TargetsResponse,
} from "@/lib/judge";
import { Badge, Button, Panel, Placeholder, Segmented, shortSvcs } from "./ui";
import { GRAPH_CELL, GRAPH_PANEL, PageHeader, PageNav, RUN_LEFT, RUN_MAIN, RUN_RIGHT } from "./Shell";
import { GraphCanvas } from "./GraphCanvas";
import { LimitNotice } from "./LimitNotice";
import { limitNotice } from "@/lib/limits";
import { CausalPathList } from "./CausalPathList";
import { Funnel } from "./Funnel";
import { Certificate } from "./Certificate";
import { HypothesisChain } from "./HypothesisChain";
import { Verdict } from "./Response";
import { StateBanner } from "./StateBanner";
import { RunLog } from "./RunLog";
import { TargetPicker } from "./TargetPicker";
import { CommitReveal } from "./CommitReveal";
import { TamperPanel } from "./TamperPanel";

interface Round {
  incidentId: string;
  commitment: string;
  blindRunId: string;
  runId: string;
  injectedAt: number; // ms
  revealAfterS: number;
  alertServiceId: string;
}

const noop = () => undefined;

// One in-flight targets request per page load: the first call may create the session, and a
// second concurrent cookie-less call (React re-running the effect in dev) would create another.
let targetsOnce: Promise<TargetsResponse> | null = null;
function loadTargets(): Promise<TargetsResponse> {
  targetsOnce ??= getTargets().catch((e) => {
    targetsOnce = null;
    throw e;
  });
  return targetsOnce;
}
const EMPTY_VIEW: GraphView = { alerting: null, symptomatic: [], suspects: [], eliminated: {}, paths: [], late: [], falsified: [] };

export default function JudgeConsole() {
  const [targets, setTargets] = useState<TargetsResponse | null>(null);
  const [targetsError, setTargetsError] = useState<string | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [changes, setChanges] = useState<ServiceChanges | null>(null);
  const [faultClass, setFaultClass] = useState<JudgeFaultClass>("bad_deploy");
  const [round, setRound] = useState<Round | null>(null);
  const [injecting, setInjecting] = useState(false);
  const [injectError, setInjectError] = useState<string | null>(null);
  const [reveal, setReveal] = useState<RevealResponse | null>(null);
  const [browserHash, setBrowserHash] = useState<string | null>(null);
  const [revealing, setRevealing] = useState(false);
  const [revealError, setRevealError] = useState<string | null>(null);
  const [blindVerdict, setBlindVerdict] = useState(false);
  const [tamperLog, setTamperLog] = useState<TamperResponse[]>([]);
  const [now, setNow] = useState(() => Date.now());
  const markBlindVerdict = useCallback(() => setBlindVerdict(true), []);

  const [attempt, setAttempt] = useState(0);
  useEffect(() => {
    let cancelled = false;
    loadTargets()
      .then((t) => !cancelled && setTargets(t))
      .catch((e) => !cancelled && setTargetsError(e instanceof Error ? e.message : "could not load services"));
    return () => {
      cancelled = true;
    };
  }, [attempt]);
  // A refused session (sandbox limits, capacity, spend cap) gets one friendly message, not raw text.
  const limit = limitNotice(targetsError);

  useEffect(() => {
    if (!selected) return;
    let cancelled = false;
    getServiceChanges(selected)
      .then((c) => !cancelled && setChanges(c))
      .catch(() => !cancelled && setChanges({ service_id: selected, changes: [], expiries: [] }));
    return () => {
      cancelled = true;
    };
  }, [selected]);

  // The reveal countdown ticks only while there is something to wait for.
  useEffect(() => {
    if (!round || reveal) return;
    const id = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(id);
  }, [round, reveal]);

  const inject = useCallback(async () => {
    if (!selected) return;
    setInjecting(true);
    setInjectError(null);
    try {
      const r = await judgeInject(selected, faultClass);
      setRound({
        incidentId: r.incident_id,
        commitment: r.commitment,
        blindRunId: r.run_id,
        runId: r.run_id,
        injectedAt: Date.now(),
        revealAfterS: r.reveal_after_s,
        alertServiceId: r.alert_service_id,
      });
      setReveal(null);
      setBrowserHash(null);
      setBlindVerdict(false);
      setTamperLog([]);
    } catch (e) {
      const msg = e instanceof Error ? e.message : "inject failed";
      setInjectError(limitNotice(msg)?.body ?? msg);
    } finally {
      setInjecting(false);
    }
  }, [selected, faultClass]);

  const doReveal = useCallback(async () => {
    if (!round) return;
    setRevealing(true);
    setRevealError(null);
    try {
      const r = await revealFault(round.incidentId);
      setReveal(r);
      recomputeCommitment(r.preimage, r.revealed.nonce).then(setBrowserHash, () => setBrowserHash(null));
    } catch (e) {
      setRevealError(e instanceof Error ? e.message : "reveal failed");
    } finally {
      setRevealing(false);
    }
  }, [round]);

  const secondsLeft = round ? Math.max(0, Math.ceil(round.revealAfterS - (now - round.injectedAt) / 1000)) : 0;
  const canReveal = !!round && (blindVerdict || secondsLeft === 0);

  const overview = useMemo(() => {
    if (!targets) return null;
    const nodes = fitNodes(
      targets.services.map((s) => ({ ...s, symptomatic: false, onset: null, signature: null, alerting: false })),
    );
    return { nodes, edges: targets.edges };
  }, [targets]);
  const [overviewFocus, setOverviewFocus] = useState<{ ids: string[]; n: number } | null>(null);
  const select = useCallback((id: string) => {
    setSelected(id);
    setOverviewFocus({ ids: [id], n: Date.now() });
  }, []);

  return (
    <div className="flex h-screen flex-col overflow-y-auto lg:grid lg:min-h-[640px] lg:grid-rows-[auto_minmax(0,1fr)] lg:overflow-hidden">
      <PageHeader
        title="Judge mode"
        status={
          <p className="text-label text-dim sm:truncate" title="Plant a fault the agent cannot see, watch it investigate blind, reveal, then tamper with the graph and re-run the proofs.">
            Plant a fault the agent cannot see, watch it investigate blind, reveal, then tamper with the graph and re-run the proofs.
          </p>
        }
        right={
          <>
            {round && <Button onClick={() => setRound(null)}>New round</Button>}
            <PageNav current="judge" />
          </>
        }
      />

      {!round && limit ? (
        <main className="min-h-0 overflow-y-auto">
          <LimitNotice
            notice={limit}
            onRetry={() => {
              setTargetsError(null);
              setAttempt((a) => a + 1);
            }}
          />
        </main>
      ) : !round ? (
        <main className="flex flex-col lg:grid lg:min-h-0 lg:grid-cols-[minmax(0,1fr)_clamp(440px,38vw,640px)]">
          <section aria-label="Service map" className="relative h-[min(55vh,460px)] min-h-[300px] shrink-0 border-b border-rule bg-deck lg:h-auto lg:min-h-0 lg:border-b-0 lg:border-r">
            {overview ? (
              <GraphCanvas
                nodes={overview.nodes}
                edges={overview.edges}
                graph={{ ...EMPTY_VIEW, suspects: selected ? [selected] : [] }}
                view="true"
                focus={overviewFocus}
                onNodeClick={select}
              />
            ) : (
              <Placeholder>{targetsError ?? "Loading your session's service map."}</Placeholder>
            )}
          </section>
          <div className={RUN_LEFT}>
            <TargetPicker
              targets={targets?.services ?? []}
              loading={!targets && !targetsError}
              error={targetsError}
              selected={selected}
              onSelect={select}
              changes={changes}
              faultClass={faultClass}
              onFaultClass={setFaultClass}
              onInject={() => void inject()}
              injecting={injecting}
              injectError={injectError}
            />
            <p className="px-1 text-meta text-faint">
              Your world is a private copy of the demo graph (s:&lt;session&gt;:world). The answer key is stored by the API, outside every graph the agent can query.
            </p>
          </div>
        </main>
      ) : (
        <JudgeRun
          key={round.runId}
          round={round}
          reveal={reveal}
          browserHash={browserHash}
          canReveal={canReveal}
          secondsLeft={secondsLeft}
          onReveal={() => void doReveal()}
          revealing={revealing}
          revealError={revealError}
          onVerdict={round.runId === round.blindRunId ? markBlindVerdict : noop}
          tamperLog={tamperLog}
          onTampered={(t) => setTamperLog((l) => [...l, t])}
          onRederived={(runId) => {
            setRound((r) => (r ? { ...r, runId } : r));
            setReveal(null); // re-open to grade the re-derived run beside the blind one
          }}
        />
      )}
    </div>
  );
}

function JudgeRun({
  round,
  reveal,
  browserHash,
  canReveal,
  secondsLeft,
  onReveal,
  revealing,
  revealError,
  onVerdict,
  tamperLog,
  onTampered,
  onRederived,
}: {
  round: Round;
  reveal: RevealResponse | null;
  browserHash: string | null;
  canReveal: boolean;
  secondsLeft: number;
  onReveal: () => void;
  revealing: boolean;
  revealError: string | null;
  onVerdict: () => void;
  tamperLog: TamperResponse[];
  onTampered: (t: TamperResponse) => void;
  onRederived: (runId: string) => void;
}) {
  const stream = useRunStream({ kind: "live", runId: round.runId }, { autoplay: true });
  const { state: s } = stream;
  const [view, setView] = useState<"true" | "known">("true");
  const [focus, setFocus] = useState<{ ids: string[]; n: number } | null>(null);
  const [selectedNode, setSelectedNode] = useState<string | null>(null);
  const [verifyResp, setVerifyResp] = useState<VerifyResponse | null>(null);
  const [results, setResults] = useState<Record<string, VerifyResult>>({});
  const [chain, setChain] = useState<ChainVerify | null>(null);
  const [verifying, setVerifying] = useState(false);
  const [rederiving, setRederiving] = useState(false);
  const [runError, setRunError] = useState<string | null>(null);
  const hasVerdict = !!s.verdict;
  useEffect(() => {
    if (hasVerdict) onVerdict();
  }, [hasVerdict, onVerdict]);

  // Incident graph at alert time (as in the console), refetched once the snapshot lands.
  const hasSnapshot = !!s.snapshot;
  const [graphResp, setGraphResp] = useState<IncidentGraphResponse | null>(null);
  useEffect(() => {
    let cancelled = false;
    getIncidentGraph(round.incidentId)
      .then((r) => !cancelled && setGraphResp(r))
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, [round.incidentId, hasSnapshot]);
  const live = useMemo(() => (graphResp ? toLiveGraph(graphResp) : null), [graphResp]);

  // Responsible ids may be changes; the canvas draws services.
  const serviceOf = useMemo(() => {
    const m = new Map<string, string>();
    graphResp?.changes.forEach((c) => m.set(c.id, c.service_id));
    tamperLog.forEach((t) => {
      if (t.created) m.set(t.created.id, t.created.service_id);
      if (t.changed) m.set(t.changed.id, t.changed.service_id);
    });
    return m;
  }, [graphResp, tamperLog]);
  const toNode = useCallback((id: string) => serviceOf.get(id) ?? id, [serviceOf]);
  const doFocus = useCallback((ids: string[]) => setFocus({ ids: ids.map(toNode), n: Date.now() }), [toNode]);

  const falsifiedNodes = useMemo(
    () => (verifyResp?.proofs ?? []).filter((p) => p.status === "FALSIFIED").flatMap((p) => p.responsible.slice(0, 4)).map(toNode),
    [verifyResp, toNode],
  );
  const graph: GraphView = useMemo(() => {
    const base = graphResp ? liveGraphView(graphResp, s) : s.graph;
    return { ...base, falsified: falsifiedNodes };
  }, [graphResp, s, falsifiedNodes]);

  const services = useMemo(() => {
    const ids = (graphResp?.nodes ?? []).map((n) => n.id);
    const labels = shortSvcs(ids);
    return ids.map((id, i) => ({ id, label: `${labels[i]}${id === round.alertServiceId ? " (alerting)" : ""}` }));
  }, [graphResp, round.alertServiceId]);
  const onset = useMemo(() => {
    const xs = (graphResp?.nodes ?? []).map((n) => n.onset).filter((x): x is number => x !== null && Number.isFinite(x) && x > 0);
    return xs.length ? Math.min(...xs) : null;
  }, [graphResp]);

  const doVerify = useCallback(async () => {
    setVerifying(true);
    setRunError(null);
    try {
      const resp = await verifyIncident(round.incidentId);
      setVerifyResp(resp);
      const mapped = mapVerify(resp, s.proofs);
      setResults(mapped.byHypothesis);
      setChain(mapped.chain);
      const first = resp.proofs.find((p) => p.status === "FALSIFIED" && p.responsible.length);
      if (first) doFocus(first.responsible.slice(0, 4));
    } catch (e) {
      setRunError(e instanceof Error ? e.message : "verify failed");
    } finally {
      setVerifying(false);
    }
  }, [round.incidentId, s.proofs, doFocus]);

  const doRederive = useCallback(async () => {
    setRederiving(true);
    setRunError(null);
    try {
      const r = await rederive(round.incidentId);
      onRederived(r.run_id);
    } catch (e) {
      setRunError(e instanceof Error ? e.message : "re-derive failed");
      setRederiving(false);
    }
  }, [round.incidentId, onRederived]);

  const onTamper = useCallback(
    async (a: TamperAction) => {
      const t = await tamper(round.incidentId, a);
      onTampered(t);
      setVerifyResp(null);
      doFocus([t.node]);
      return t;
    },
    [round.incidentId, onTampered, doFocus],
  );

  const banners = deriveBanners(s);
  if (stream.status === "error") banners.push({ kind: "failed", tone: "bad", title: "Stream lost", body: stream.error ?? "The event stream could not be re-established." });
  const outcome = s.verdict?.outcome;
  const rederived = round.runId !== round.blindRunId;
  const finished = s.done;

  return (
    <div className="flex flex-col lg:grid lg:min-h-0 lg:grid-rows-[auto_auto_minmax(0,1fr)]">
      <div className="flex min-h-10 flex-wrap items-center gap-3 border-b border-rule bg-deck px-4 py-1.5 sm:px-5">
        <span className="font-mono text-label text-ink">{round.incidentId}</span>
        <span className="truncate text-label text-dim">{s.incident?.title ?? "starting"}</span>
        <Badge tone={rederived ? "info" : "muted"}>{rederived ? "RE-DERIVED RUN" : "BLIND RUN"}</Badge>
        {s.phase && <Badge tone="muted">{s.phase}</Badge>}
        {outcome && <Badge tone={outcome === "DIAGNOSED" ? "verified" : outcome === "INCONCLUSIVE" ? "drifted" : "falsified"}>VERDICT {outcome}</Badge>}
        {isDegraded(s) && <Badge tone="degraded">DEGRADED</Badge>}
        <div className="ml-auto">
          <Segmented
            name="jview"
            legend="Evidence view"
            value={view}
            onChange={setView}
            options={[{ value: "known", label: "as-known" }, { value: "true", label: "as-true" }]}
          />
        </div>
        <span className="font-mono text-meta tabular-nums text-dim" aria-live="polite">{stream.status} · {stream.total} events</span>
      </div>
      <StateBanner banners={banners} />

      <main className={RUN_MAIN}>
        <div className={RUN_LEFT}>
          <Panel aria-label="Graph" className={GRAPH_PANEL}>
            <div className={GRAPH_CELL}>
              {live ? (
                <GraphCanvas
                  nodes={live.nodes}
                  edges={live.edges}
                  graph={graph}
                  view={view}
                  focus={focus}
                  settled={finished || !!outcome}
                  onNodeClick={(id) => {
                    setSelectedNode(id);
                    doFocus([id]);
                  }}
                />
              ) : (
                <Placeholder>Loading the service graph at alert time.</Placeholder>
              )}
            </div>
            <CausalPathList graph={graph} onFocus={doFocus} />
          </Panel>
          <Panel className="shrink-0">
            <Funnel steps={s.funnel} onFocus={doFocus} incidentId={round.incidentId} />
          </Panel>
          <details className="shrink-0 border border-rule bg-deck">
            <summary className="cursor-pointer px-inset py-2 text-label font-medium text-dim hover:text-ink">Run log ({s.toolCalls.length})</summary>
            <div className="max-h-[32vh] overflow-y-auto border-t border-rule">
              <RunLog calls={s.toolCalls} t0={s.startedTs} />
            </div>
          </details>
        </div>

        <div className={RUN_RIGHT}>
          <CommitReveal
            commitment={round.commitment}
            incidentId={round.incidentId}
            canReveal={canReveal}
            secondsLeft={secondsLeft}
            reveal={reveal}
            browserHash={browserHash}
            onReveal={onReveal}
            revealing={revealing}
            error={revealError}
          />
          <Verdict s={s} view={view} />
          <TamperPanel
            incidentId={round.incidentId}
            runId={round.runId}
            onset={onset}
            services={services}
            edges={graphResp?.edges ?? []}
            changes={graphResp?.changes ?? []}
            selectedNode={selectedNode}
            disabled={!finished}
            onSubmit={onTamper}
            log={tamperLog}
            onVerify={() => void doVerify()}
            verifying={verifying}
            verify={verifyResp}
            onRederive={() => void doRederive()}
            rederiving={rederiving}
            onFocusNode={(n) => doFocus([n])}
          />
          {runError && <p role="alert" className="border-b border-rule px-inset py-2 text-label text-falsified">{runError}</p>}
          <Certificate cert={s.certificate} view={view} onFocus={doFocus} />
          <div className="border-t border-rule">
            <HypothesisChain
              proofs={s.proofs}
              results={results}
              running={verifying ? Object.fromEntries(s.proofs.map((p) => [p.hypothesis_id, true])) : {}}
              anchored={s.done || !!s.verdict?.chain_head}
              chain={chain}
              onVerify={() => void doVerify()}
              onVerifyAll={() => void doVerify()}
              onFocusNode={(n) => doFocus([n])}
              incidentId={round.incidentId}
            />
          </div>
        </div>
      </main>
    </div>
  );
}
