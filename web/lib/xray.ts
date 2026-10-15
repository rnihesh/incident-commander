// Query X-ray helpers: the plan rows the console draws and the note tying them to the design.
import type { XrayOp } from "./api";

/** Why the plan matters (R7). Verified on FalkorDB: see docs/notes/day0-spikes.md, S2. */
export const XRAY_NOTE =
  "FalkorDB applies a WHERE on a variable-length match after the traversal: the Filter sits above the " +
  "Conditional Variable Length Traverse, so every path is expanded first and discarded later. That is why " +
  "Incident Commander materializes an as-of snapshot of the graph at alert time instead of filtering valid " +
  "time inside the traversal: the snapshot only holds edges that existed then, so no hop is walked to be thrown away.";

export interface XrayRow {
  depth: number;
  name: string;
  args: string | null;
  records: number | null;
  ms: number | null;
}

function flatten(op: XrayOp, depth = 0, out: XrayRow[] = []): XrayRow[] {
  out.push({ depth, name: op.name, args: op.args, records: op.records, ms: op.ms });
  for (const c of op.children) flatten(c, depth + 1, out);
  return out;
}

/** The EXPLAIN plan in pre-order with the PROFILE's records/ms merged in; when the two
 *  trees differ in shape (the planner chose differently between calls), the profile tree. */
export function xrayRows(explain: XrayOp, profile: XrayOp): XrayRow[] {
  const planned = flatten(explain);
  const ran = flatten(profile);
  const same = planned.length === ran.length && planned.every((r, i) => r.name === ran[i].name && r.depth === ran[i].depth);
  if (!same) return ran;
  return planned.map((r, i) => ({ ...r, records: ran[i].records, ms: ran[i].ms }));
}

/** Child-index path from the root to the first operator (pre-order) matching `pred`; [] is the root. */
export function findOperatorPath(root: XrayOp, pred: (o: XrayOp) => boolean, path: number[] = []): number[] | null {
  if (pred(root)) return path;
  for (let i = 0; i < root.children.length; i++) {
    const hit = findOperatorPath(root.children[i], pred, [...path, i]);
    if (hit) return hit;
  }
  return null;
}

/** Pre-order row index of the operator at `path`, matching `xrayRows`' row order. */
function rowIndexOf(root: XrayOp, path: number[]): number {
  let idx = 0;
  let node = root;
  for (const step of path) {
    idx += 1;
    for (let i = 0; i < step; i++) idx += flatten(node.children[i]).length;
    node = node.children[step];
  }
  return idx;
}

/** Rows to flag in the naive plan: the Conditional Variable Length Traverse and the nearest Filter
 *  above it, which FalkorDB applies only after the traversal. Null when the plan has no such traverse. */
export function naiveHighlight(root: XrayOp): { filter: number | null; traverse: number } | null {
  const tPath = findOperatorPath(root, (o) => o.name === "Conditional Variable Length Traverse");
  if (!tPath) return null;
  let node = root;
  let filterPath: number[] | null = null;
  for (let d = 0; d < tPath.length; d++) {
    if (node.name === "Filter") filterPath = tPath.slice(0, d);
    node = node.children[tPath[d]];
  }
  return { filter: filterPath ? rowIndexOf(root, filterPath) : null, traverse: rowIndexOf(root, tPath) };
}

/** The ledger seq a proof's X-ray asks for: the trailing number of `hyp-{iid}-{seq}`. */
export function proofSeq(id: string): number | null {
  const m = /-(\d+)$/.exec(id);
  return m ? Number(m[1]) : null;
}

export function fmtMs(ms: number | null): string {
  if (ms === null) return "";
  return ms >= 1 ? `${ms.toFixed(2)} ms` : `${ms.toFixed(3)} ms`;
}
