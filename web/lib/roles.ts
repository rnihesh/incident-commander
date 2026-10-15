import type { GraphView } from "./reducer";
import type { NodeRole } from "./mock/hero-graph";

/** Highest-priority role wins: alerting > path > suspect > symptomatic > eliminated. */
export function roleOf(id: string, g: GraphView): NodeRole {
  if (g.alerting === id) return "alerting";
  if (g.paths.some((p) => p.nodes.includes(id))) return "path";
  if (g.suspects.includes(id)) return "suspect";
  if (g.symptomatic.includes(id)) return "symptomatic";
  if (g.eliminated[id]) return "eliminated";
  return "plain";
}

export const ROLE_LABEL: Record<NodeRole, string> = {
  alerting: "Alerting service",
  symptomatic: "Symptomatic",
  suspect: "Suspect",
  eliminated: "Eliminated",
  path: "Causal-path hop",
  plain: "In closure, not implicated",
};
