import type { EliminatedChange } from "./payloads";

export type ExonerateAnswer =
  | { kind: "path"; change: string; nodes: string[] }
  | { kind: "receipt"; change: string; service: string; clause: string; hash: string };

const rec = (v: unknown): Record<string, unknown> | null =>
  v && typeof v === "object" && !Array.isArray(v) ? (v as Record<string, unknown>) : null;
const str = (v: unknown): string | undefined => (typeof v === "string" && v ? v : undefined);

const nodeId = (n: unknown): string | undefined => str(n) ?? str(rec(n)?.id) ?? str(rec(n)?.service_id) ?? str(rec(n)?.service);

/**
 * Read the exonerate ToolResult.result defensively: either a causal path
 * (array of nodes under path / causal_path / nodes) or a receipt naming the
 * failing clause. Returns null if neither is recognisable.
 */
export function parseExonerate(result: unknown, change: string, row?: EliminatedChange): ExonerateAnswer | null {
  const r = rec(result);
  if (!r) return null;
  const rcpt = rec(r.receipt) ?? (r.exonerated === true ? r : null);
  const pathRaw = r.causal_path ?? r.path ?? r.nodes;
  if (!rcpt && Array.isArray(pathRaw) && pathRaw.length) {
    const nodes = pathRaw.map(nodeId).filter((x): x is string => !!x);
    if (nodes.length) return { kind: "path", change, nodes };
  }
  if (rcpt) {
    const clause = str(rcpt.failing_clause) ?? str(rcpt.failed_clause) ?? str(rcpt.clause) ?? row?.clause;
    if (!clause) return null;
    return {
      kind: "receipt",
      change,
      service: str(rcpt.service) ?? str(rcpt.service_id) ?? row?.service ?? "",
      clause,
      hash: str(rcpt.hash) ?? str(rcpt.receipt_hash) ?? str(r.hash) ?? "",
    };
  }
  return null;
}
