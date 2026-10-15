// Storm mode (U17): the event stream of one alert-storm collapse run, folded into the
// state the Storm panel draws. Mirrors api/src/ic/api/routes_storm.py.
import type { RunEvent, ToolResult } from "./events";
import type { GraphView } from "./reducer";
import type { GraphEdge, GraphNode } from "./mock/hero-graph";
import { fitNodes } from "./liveGraph";

export interface StormChange {
  id: string;
  kind: string;
  service_id: string;
  summary: string | null;
  event_time: number;
}

export interface StormJoint {
  flag_change: string;
  config_change: string;
  flag: string;
  config: string;
  service_id: string;
  solo: Record<string, { from: number; to: number; seconds: number; lead_s: number } | null>;
}

export interface StormIncident {
  component: number;
  symptomatic: string[];
  alerts: number;
  first_page: number;
  explaining: StormChange[];
  jointly_caused: boolean;
  joint: StormJoint | null;
  alternatives: string[][];
  unexplained: string[];
  paths: { label: string; nodes: string[] }[];
  checked: number;
}

export interface StormAreaNode {
  id: string;
  name: string;
  x: number;
  y: number;
  symptomatic: boolean;
}

export interface StormState {
  lastEventId: number;
  stormId: string | null;
  title: string | null;
  total: number;
  received: number;
  servicesPaging: number;
  batches: number[]; // pages received per intake batch
  grouped: { components: { component: number; alerts: number; services: string[] }[]; symptomatic_services: number; candidates_checked: number } | null;
  incidents: StormIncident[];
  chokepoints: { service_id: string; score: number }[];
  area: { nodes: StormAreaNode[]; edges: GraphEdge[] } | null;
  collapsed: { pages_in: number; incidents: number; jointly_caused: number; tool_ms: number } | null;
  tool: ToolResult | null;
  error: string | null;
  done: boolean;
}

export const initialStorm: StormState = {
  lastEventId: -1,
  stormId: null,
  title: null,
  total: 0,
  received: 0,
  servicesPaging: 0,
  batches: [],
  grouped: null,
  incidents: [],
  chokepoints: [],
  area: null,
  collapsed: null,
  tool: null,
  error: null,
  done: false,
};

export function reduceStorm(s: StormState, ev: RunEvent): StormState {
  if (ev.id <= s.lastEventId) return s;
  const base = { ...s, lastEventId: ev.id };
  const d = ev.data as Record<string, unknown>;
  switch (ev.type) {
    case "run_started":
      return { ...base, stormId: (d.storm_id as string) ?? null, title: (d.title as string) ?? null, total: (d.pages as number) ?? 0 };
    case "tool_call":
      return { ...base, tool: d as unknown as ToolResult };
    case "storm": {
      switch (d.phase) {
        case "intake": {
          const received = d.received as number;
          return {
            ...base,
            total: (d.total as number) ?? base.total,
            received,
            servicesPaging: d.services_paging as number,
            batches: [...base.batches, received - base.received],
          };
        }
        case "grouped":
          return { ...base, grouped: d as unknown as StormState["grouped"] };
        case "incident":
          return { ...base, incidents: [...base.incidents, d as unknown as StormIncident] };
        case "chokepoints":
          return {
            ...base,
            chokepoints: (d.chokepoints as StormState["chokepoints"]) ?? [],
            area: (d.area as StormState["area"]) ?? null,
          };
        case "collapsed":
          return { ...base, collapsed: d as unknown as StormState["collapsed"] };
        default:
          return base;
      }
    }
    case "error":
      return { ...base, error: (d.message as string) ?? "storm run failed" };
    case "done":
      return { ...base, done: true };
    default:
      return base;
  }
}

export const reduceStormAll = (events: RunEvent[]) => events.reduce(reduceStorm, initialStorm);

/** What the counter shows: pages so far, then pages -> incidents once grouped. */
export function counter(s: StormState): { pages: number; incidents: number | null } {
  const pages = s.collapsed?.pages_in ?? (s.grouped ? s.total : s.received);
  const incidents = s.collapsed?.incidents ?? (s.grouped ? s.grouped.components.length : null);
  return { pages, incidents };
}

/** Canvas inputs for the storm area; ``focus`` (a component number) filters it to that
 * incident's services and the services on its causal paths. */
export function stormCanvas(s: StormState, focus: number | null): { nodes: GraphNode[]; edges: GraphEdge[]; graph: GraphView } {
  const area = s.area ?? { nodes: [], edges: [] };
  const inc = focus === null ? null : s.incidents.find((i) => i.component === focus) ?? null;
  const incidents = inc ? [inc] : s.incidents;
  const keep = inc ? new Set([...inc.symptomatic, ...inc.paths.flatMap((p) => p.nodes)]) : null;
  const shown = area.nodes.filter((n) => !keep || keep.has(n.id));
  const ids = new Set(shown.map((n) => n.id));
  const fitted = fitNodes(shown.map((n) => ({ ...n, onset: null, signature: null, alerting: false })));
  const explainingHomes = incidents.flatMap((i) => i.explaining.map((c) => c.service_id));
  return {
    nodes: fitted,
    edges: area.edges.filter((e) => ids.has(e.source) && ids.has(e.target)),
    graph: {
      alerting: null,
      symptomatic: inc ? inc.symptomatic : area.nodes.filter((n) => n.symptomatic).map((n) => n.id),
      suspects: [...new Set(explainingHomes)],
      eliminated: {},
      paths: incidents.flatMap((i) => i.paths),
      late: [],
      falsified: [],
      chokepoints: s.chokepoints.map((c) => c.service_id).filter((id) => ids.has(id)),
    },
  };
}
