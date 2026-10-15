// Maps GET /api/incidents/{id}/graph plus reducer state onto the canvas inputs.
import type { IncidentGraphResponse } from "./api";
import type { ConsoleState, GraphView } from "./reducer";
import type { GraphEdge, GraphNode } from "./mock/hero-graph";

export const CANVAS_W = 1000;
export const CANVAS_H = 600;
export const CANVAS_PAD = 40;

/** Fit x,y into the padded canvas box, preserving aspect ratio and centering. */
export function fitNodes(
  nodes: IncidentGraphResponse["nodes"],
  w = CANVAS_W,
  h = CANVAS_H,
  pad = CANVAS_PAD,
): GraphNode[] {
  if (!nodes.length) return [];
  const xs = nodes.map((n) => n.x);
  const ys = nodes.map((n) => n.y);
  const minX = Math.min(...xs);
  const minY = Math.min(...ys);
  const spanX = Math.max(...xs) - minX;
  const spanY = Math.max(...ys) - minY;
  const availW = w - 2 * pad;
  const availH = h - 2 * pad;
  const k = Math.min(spanX > 0 ? availW / spanX : Infinity, spanY > 0 ? availH / spanY : Infinity);
  const scale = Number.isFinite(k) ? k : 1;
  const offX = pad + (availW - spanX * scale) / 2;
  const offY = pad + (availH - spanY * scale) / 2;
  return nodes.map((n) => ({
    id: n.id,
    name: n.name,
    tier: n.tier ?? 0,
    x: offX + (n.x - minX) * scale,
    y: offY + (n.y - minY) * scale,
  }));
}

export interface LiveGraph {
  nodes: GraphNode[];
  edges: GraphEdge[];
}

export function toLiveGraph(resp: Pick<IncidentGraphResponse, "nodes" | "edges">): LiveGraph {
  const ids = new Set(resp.nodes.map((n) => n.id));
  return {
    nodes: fitNodes(resp.nodes),
    edges: resp.edges.filter((e) => ids.has(e.source) && ids.has(e.target)).map((e) => ({ source: e.source, target: e.target })),
  };
}

const uniq = (xs: string[]) => [...new Set(xs)];

/** Node roles: response flags first, then whatever the reducer has learned from the stream. */
export function liveGraphView(resp: IncidentGraphResponse, s: ConsoleState): GraphView {
  const g = s.graph;
  const serviceOf = new Map(resp.changes.map((c) => [c.id, c.service_id]));
  let suspects = g.suspects;
  if (!suspects.length && !s.verdict) {
    const last = s.funnel[s.funnel.length - 1];
    suspects = uniq((last?.top_suspects ?? []).map((c) => serviceOf.get(c)).filter((x): x is string => !!x));
  }
  // Late-arriving: stream snapshot flags plus changes the certificate or funnel marked late.
  const late = uniq([
    ...g.late,
    ...(s.certificate?.checked ?? []).filter((c) => c.late).map((c) => c.service),
    ...s.funnel.flatMap((f) => f.eliminated.filter((e) => e.late).map((e) => e.service)),
  ]);
  return {
    alerting: g.alerting ?? resp.nodes.find((n) => n.alerting)?.id ?? resp.alert_service_id ?? null,
    symptomatic: uniq([...g.symptomatic, ...resp.nodes.filter((n) => n.symptomatic).map((n) => n.id)]),
    suspects,
    eliminated: g.eliminated,
    paths: g.paths,
    late,
    lateEdges: g.lateEdges ?? [],
    falsified: g.falsified,
  };
}

/**
 * Which incident graph the console draws. Live runs use the graph fetched for their incident;
 * recorded playback uses the graph saved with the recording (`undefined` = still loading,
 * `null` = unavailable), unless it is for a different incident than the one playing. A null
 * `resp` means draw the mock hero graph (scripted scenarios, or a failed fetch).
 */
export function pickGraph(o: {
  kind: "mock" | "live" | "recorded";
  incidentId: string | null;
  live: IncidentGraphResponse | null;
  recorded: IncidentGraphResponse | null | undefined;
}): { resp: IncidentGraphResponse | null; pending: boolean } {
  if (o.kind === "live") return { resp: o.live, pending: false };
  if (o.kind !== "recorded") return { resp: null, pending: false };
  if (o.recorded === undefined) return { resp: null, pending: true };
  const matches = o.recorded !== null && (o.incidentId === null || o.recorded.incident_id === o.incidentId);
  return { resp: matches ? o.recorded : null, pending: false };
}
