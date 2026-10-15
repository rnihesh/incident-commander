import type { RunEvent, ToolResult } from "./events";
import {
  CLAUSES,
  type ActionData,
  type CertificateData,
  type Clause,
  type ErrorData,
  type FunnelData,
  type HandoffData,
  type HypothesisData,
  type IncidentState,
  type LateEdge,
  type MemoryData,
  type NarrationData,
  type RecommendationRef,
  type PathRef,
  type PrecedentMatch,
  type PrecedentRecord,
  type ProofData,
  type ReflexData,
  type RehearsalData,
  type RunStartedData,
  type SnapshotData,
  type StateData,
  type VerdictData,
} from "./payloads";

export interface ToolCallEntry extends ToolResult {
  eventId: number;
  ts: number;
  agent: string;
}

export interface ProofEntry extends ProofData {
  eventId: number;
  hypothesis?: HypothesisData;
}

export interface ReflexView extends ReflexData {
  superseded: boolean;
  diff: { added: string[]; removed: string[] } | null;
}

export interface GraphView {
  alerting: string | null;
  symptomatic: string[];
  suspects: string[];
  eliminated: Record<string, { clause: Clause; change: string }>;
  paths: { label: string; nodes: string[] }[];
  late: string[];
  /** Late DEPENDS_ON edges on the impact area (listed, never used to hide nodes). */
  lateEdges?: LateEdge[];
  falsified: string[]; // nodes introduced/flagged by a FALSIFIED verification
  chokepoints?: string[]; // storm mode: seeded-betweenness chokepoints (U17)
}

export type BannerKind =
  | "empty"
  | "investigating"
  | "diagnosed"
  | "inconclusive"
  | "refused"
  | "failed"
  | "degraded"
  | "recorded"
  | "rate_limited"
  | "session_expired"
  | "action_error";

export interface ConsoleState {
  lastEventId: number;
  startedTs: number | null;
  lastTs: number | null;
  runId: string | null;
  incident: RunStartedData | null;
  phase: IncidentState | null;
  reflex: ReflexView | null;
  snapshot: SnapshotData | null;
  toolCalls: ToolCallEntry[];
  proofs: ProofEntry[];
  pendingHypotheses: HypothesisData[];
  funnel: FunnelData[];
  certificate: CertificateData | null;
  verdict: VerdictData | null;
  degradedReason: string | null;
  precedent: { text: string; matches: PrecedentMatch[] }[];
  narration: string[];
  handoff: HandoffData | null;
  rehearsal: RehearsalData | null;
  actions: ActionData[];
  memory: MemoryData | null;
  recommendations: RecommendationRef[];
  error: ErrorData | null;
  actionErrors: ErrorData[];
  done: boolean;
  doneMs: number | null;
  graph: GraphView;
  unknownEvents: number;
}

export const initialState: ConsoleState = {
  lastEventId: -1,
  startedTs: null,
  lastTs: null,
  runId: null,
  incident: null,
  phase: null,
  reflex: null,
  snapshot: null,
  toolCalls: [],
  proofs: [],
  pendingHypotheses: [],
  funnel: [],
  certificate: null,
  verdict: null,
  degradedReason: null,
  precedent: [],
  narration: [],
  handoff: null,
  rehearsal: null,
  actions: [],
  memory: null,
  recommendations: [],
  error: null,
  actionErrors: [],
  done: false,
  doneMs: null,
  graph: {
    alerting: null,
    symptomatic: [],
    suspects: [],
    eliminated: {},
    paths: [],
    late: [],
    falsified: [],
  },
  unknownEvents: 0,
};

const METHOD_OF = { vector: "semantic", fulltext: "fuzzy", fingerprint: "path_shape" } as const;

function methodScore(v: unknown): number | null {
  if (typeof v === "number" && Number.isFinite(v)) return v;
  if (v === true) return 1;
  if (v && typeof v === "object" && typeof (v as { score?: unknown }).score === "number") return (v as { score: number }).score;
  return null;
}

/** Fold a backend precedent record (per-method scores) into the single-method match shape. */
export function toMatch(p: PrecedentRecord): PrecedentMatch {
  const methods: NonNullable<PrecedentMatch["methods"]> = {};
  let best: keyof typeof METHOD_OF = "vector";
  let bestScore = -1;
  for (const k of Object.keys(METHOD_OF) as (keyof typeof METHOD_OF)[]) {
    const sc = methodScore(p.methods?.[k]);
    if (sc === null) continue;
    methods[k] = sc;
    if (sc > bestScore) {
      best = k;
      bestScore = sc;
    }
  }
  return {
    incident: p.incident_id,
    title: p.title,
    method: METHOD_OF[best],
    score: Math.max(bestScore, 0),
    cause: p.remediation ?? "",
    postmortem_id: p.postmortem_id,
    methods,
    remediation: p.remediation,
    resolved_minutes: p.resolved_minutes,
  };
}

const clauseRank = (c: Clause) => CLAUSES.indexOf(c);

function diffSuspects(prev: string[], next: string[]) {
  const added = next.filter((x) => !prev.includes(x));
  const removed = prev.filter((x) => !next.includes(x));
  return added.length || removed.length ? { added, removed } : null;
}

type Data<T> = T; // readability: payloads are trusted shapes, checked loosely below

export function reduce(state: ConsoleState, ev: RunEvent): ConsoleState {
  if (ev.id <= state.lastEventId) return state; // replayed / duplicate delivery
  const base: ConsoleState = {
    ...state,
    lastEventId: ev.id,
    startedTs: state.startedTs ?? ev.ts,
    lastTs: ev.ts,
    runId: state.runId ?? ev.run_id,
  };
  const d = ev.data as Record<string, unknown>;

  switch (ev.type) {
    case "run_started": {
      const data = d as unknown as Data<RunStartedData>;
      return {
        ...base,
        incident: data,
        phase: base.phase ?? "OPEN",
        graph: { ...base.graph, alerting: data.alert?.service ?? null },
      };
    }
    case "reflex": {
      const data = d as unknown as ReflexData;
      return { ...base, reflex: { ...data, superseded: false, diff: null } };
    }
    case "snapshot": {
      const data = d as unknown as SnapshotData;
      return {
        ...base,
        snapshot: data,
        graph: {
          ...base.graph,
          symptomatic: data.symptomatic ?? [],
          late: data.late_arriving ?? [],
          lateEdges: data.late_edges ?? [],
        },
      };
    }
    case "tool_call": {
      const data = d as unknown as ToolResult;
      return {
        ...base,
        toolCalls: [...base.toolCalls, { ...data, eventId: ev.id, ts: ev.ts, agent: ev.agent }],
      };
    }
    case "hypothesis": {
      const data = d as unknown as HypothesisData;
      return { ...base, pendingHypotheses: [...base.pendingHypotheses, data] };
    }
    case "proof": {
      const data = d as unknown as ProofData;
      const hyp = base.pendingHypotheses.find((h) => h.id === data.hypothesis_id);
      return {
        ...base,
        proofs: [...base.proofs, { ...data, eventId: ev.id, hypothesis: hyp }],
        pendingHypotheses: base.pendingHypotheses.filter((h) => h.id !== data.hypothesis_id),
      };
    }
    case "funnel": {
      const data = d as unknown as FunnelData;
      let reflex = base.reflex;
      if (reflex && !reflex.superseded) {
        reflex = {
          ...reflex,
          superseded: true,
          diff: diffSuspects(
            reflex.suspects.map((s) => s.change),
            data.top_suspects ?? [],
          ),
        };
      }
      const eliminated = { ...base.graph.eliminated };
      for (const e of data.eliminated ?? []) {
        const prev = eliminated[e.service];
        if (!prev || clauseRank(e.clause) >= clauseRank(prev.clause)) {
          eliminated[e.service] = { clause: e.clause, change: e.change };
        }
      }
      return {
        ...base,
        reflex,
        funnel: [...base.funnel, data],
        graph: { ...base.graph, eliminated },
      };
    }
    case "certificate": {
      return { ...base, certificate: d as unknown as CertificateData };
    }
    case "verdict": {
      const data = d as unknown as VerdictData;
      const paths = (data.paths ?? []).map((p: PathRef) => ({ label: p.label, nodes: p.nodes }));
      return {
        ...base,
        verdict: data,
        graph: {
          ...base.graph,
          paths,
          suspects: (data.paths ?? []).map((p) => p.service),
        },
      };
    }
    case "state": {
      return { ...base, phase: (d as unknown as StateData).state };
    }
    case "handoff":
      return { ...base, handoff: d as unknown as HandoffData };
    case "rehearsal":
      return { ...base, rehearsal: d as unknown as RehearsalData };
    case "action": {
      const a = d as unknown as ActionData;
      return { ...base, actions: [...base.actions, a] };
    }
    case "memory":
      return { ...base, memory: d as unknown as MemoryData };
    case "narration": {
      const n = d as unknown as NarrationData;
      const degradedReason = n.degraded_reason ?? base.degradedReason;
      if (n.topic === "precedent") {
        const matches = [...(n.matches ?? []), ...(n.precedents ?? []).map(toMatch)];
        const text = n.text ?? (matches.length ? "Similar past incidents." : "No similar past incident found.");
        return {
          ...base,
          degradedReason,
          precedent: [...base.precedent, { text, matches }],
          narration: [...base.narration, text],
        };
      }
      if (n.topic === "recommendation") {
        const recs = n.recommendations ?? [];
        const seen = new Set(base.recommendations.map((r) => r.id));
        return {
          ...base,
          degradedReason,
          recommendations: [...base.recommendations, ...recs.filter((r) => !seen.has(r.id))],
          narration: n.text ? [...base.narration, n.text] : base.narration,
        };
      }
      return { ...base, degradedReason, narration: n.text ? [...base.narration, n.text] : base.narration };
    }
    case "error": {
      const e = d as unknown as ErrorData;
      if (e.code === "action_failed") {
        return { ...base, actionErrors: [...base.actionErrors, e] };
      }
      return { ...base, error: e, phase: e.code === "failed" ? "FAILED" : base.phase };
    }
    case "done":
      return { ...base, done: true, doneMs: (d as { ms?: number }).ms ?? null };
    default:
      // Unknown event types must not break the stream (forward compatibility).
      return { ...state, lastEventId: ev.id, unknownEvents: state.unknownEvents + 1 };
  }
}

export function reduceAll(events: RunEvent[], from: ConsoleState = initialState): ConsoleState {
  return events.reduce(reduce, from);
}

export interface Banner {
  kind: BannerKind;
  tone: "info" | "ok" | "warn" | "bad" | "muted" | "degraded";
  title: string;
  body: string;
}

const DEGRADED_TAIL = "The deterministic funnel produced this verdict. Every proof below is still re-runnable.";
const DEGRADED_COPY: Record<string, string> = {
  no_llm: "No LLM configured: the deterministic funnel produced this verdict. Every proof below is still re-runnable.",
  step_cap: `The agent loop hit its step cap. ${DEGRADED_TAIL}`,
  spend_cap: `The agent loop hit its spend cap. ${DEGRADED_TAIL}`,
  llm_error: `The LLM call failed. ${DEGRADED_TAIL}`,
  timeout: `The agent loop timed out. ${DEGRADED_TAIL}`,
};

/** Banner copy for a degraded verdict; unknown or missing reasons get a neutral message. */
export function degradedCopy(reason?: string | null): string {
  return (reason && DEGRADED_COPY[reason]) || `The agent loop did not complete. ${DEGRADED_TAIL}`;
}

export const isDegraded = (s: ConsoleState) => !!s.verdict && !!(s.verdict.degraded || s.verdict.degraded_reason || s.degradedReason);

/** Ordered banners for the current state; the first is the primary one. */
export function deriveBanners(s: ConsoleState): Banner[] {
  const out: Banner[] = [];
  if (s.error?.code === "rate_limited") {
    const wait = s.error.retry_after_s ? ` Try again in ${s.error.retry_after_s} s.` : "";
    out.push({
      kind: "rate_limited",
      tone: "warn",
      title: "Investigation cap reached",
      body: `${s.error.message}${wait} Recorded runs stay available in the scenario list.`,
    });
    return out;
  }
  if (s.error?.code === "session_expired") {
    out.push({
      kind: "session_expired",
      tone: "bad",
      title: "Session expired",
      body: `${s.error.message} Start a new session; this world and its proof chain are gone.`,
    });
    return out;
  }
  if (!s.incident) {
    out.push({
      kind: "empty",
      tone: "muted",
      title: "No incident loaded",
      body: "Inject a fault to start an investigation, or pick a recorded scenario above.",
    });
    return out;
  }
  if (s.incident.recorded) {
    out.push({
      kind: "recorded",
      tone: "info",
      title: "Recorded run",
      body: "Replaying a saved event stream. Queries are real; nothing is executing against a live graph.",
    });
  }
  const v = s.verdict;
  if (s.error?.code === "failed" || v?.outcome === "FAILED") {
    out.push({
      kind: "failed",
      tone: "bad",
      title: "Investigation failed",
      body:
        s.error?.message ??
        "No verdict could be reached, even with the deterministic fallback. The on-call has been paged.",
    });
  } else if (s.rehearsal?.result === "REFUSED") {
    const via = s.rehearsal.surviving
      ? ` ${s.rehearsal.surviving.change} still explains part of the symptoms via ${s.rehearsal.surviving.path.join(" > ")}.`
      : "";
    out.push({
      kind: "refused",
      tone: "bad",
      title: "Rollback refused",
      body: `Rehearsal on the cloned world did not clear the fault.${via} No PR was opened; the on-call was paged.`,
    });
  } else if (v?.outcome === "INCONCLUSIVE" || s.phase === "INCONCLUSIVE") {
    out.push({
      kind: "inconclusive",
      tone: "warn",
      title: "Inconclusive: rival causal paths",
      body: `${v?.paths.length ?? 2} suspects each explain the full symptom set. No PR was opened; the on-call was paged with both paths.`,
    });
  } else if (v?.outcome === "DIAGNOSED") {
    out.push({
      kind: "diagnosed",
      tone: "ok",
      title: `Diagnosed: ${v.root_cause?.change ?? "root cause found"}`,
      body: v.headline,
    });
  } else {
    out.push({
      kind: "investigating",
      tone: "info",
      title: "Investigating",
      body: s.funnel.length
        ? `Funnel at ${s.funnel[s.funnel.length - 1].after} candidate change${s.funnel[s.funnel.length - 1].after === 1 ? "" : "s"}.`
        : s.snapshot
          ? "Snapshot built. Eliminating changes."
          : "Building the as-of snapshot.",
    });
  }
  const reason = v?.degraded_reason ?? s.degradedReason;
  if (v && (v.degraded || reason)) {
    out.push({ kind: "degraded", tone: "degraded", title: "Degraded", body: degradedCopy(reason) });
  }
  for (const e of s.actionErrors) {
    out.push({
      kind: "action_error",
      tone: "bad",
      title: `Action failed${e.action ? `: ${e.action}` : ""}`,
      body: `${e.message} Retry from the action list; the diagnosis is unaffected.`,
    });
  }
  return out;
}
