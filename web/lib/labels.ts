import type { GraphView } from "./reducer";
import type { NodeRole } from "./mock/hero-graph";

/** One node label waiting to be drawn. Coordinates and sizes share one unit (graph units on the
 *  canvas); `r` is the node's radius, `w`/`h` the text box. Lower `rank` draws first. */
export interface LabelCandidate {
  id: string;
  x: number;
  y: number;
  r: number;
  w: number;
  h: number;
  rank: number;
}

/** Where a label landed: the text's top-centre anchor and the box it occupies. */
export interface PlacedLabel {
  id: string;
  tx: number;
  ty: number;
  box: Box;
}

export interface Box {
  x0: number;
  y0: number;
  x1: number;
  y1: number;
}

const hit = (a: Box, b: Box) => a.x0 < b.x1 && b.x0 < a.x1 && a.y0 < b.y1 && b.y0 < a.y1;

/** Label priority: alerting > suspect (the root) > causal path > symptomatic > the rest. */
export function labelRank(id: string, role: NodeRole, g: GraphView): number {
  if (role === "alerting") return 0;
  if (g.suspects.includes(id)) return 1;
  if (role === "path") return 2;
  if (role === "symptomatic") return 3;
  return 4;
}

/** Greedy label placement. In rank order, each label tries below its node, then above, right and
 *  left; the first slot that clears every already-placed label (and, if possible, every labelled
 *  node's disc) wins. A label with no free slot is skipped (hover still names the node). `gap` is
 *  the node-to-text gap. With `view` (the visible canvas, same units) a slot that stays on screen
 *  beats one that would run off the edge. */
export function placeLabels(cands: LabelCandidate[], gap: number, view?: Box): PlacedLabel[] {
  const order = [...cands].sort((a, b) => a.rank - b.rank || a.id.localeCompare(b.id));
  const discs: Box[] = order.map((c) => ({ x0: c.x - c.r, y0: c.y - c.r, x1: c.x + c.r, y1: c.y + c.r }));
  const placed: PlacedLabel[] = [];
  for (const c of order) {
    const below = c.y + c.r + gap;
    const above = c.y - c.r - gap - c.h;
    const mid = c.y - c.h / 2;
    const side = c.r + gap + c.w / 2;
    const slots: [number, number][] = [
      [c.x, below],
      [c.x, above],
      [c.x + side, mid],
      [c.x - side, mid],
    ];
    const boxes = slots.map(([tx, ty]) => ({ tx, ty, box: { x0: tx - c.w / 2, y0: ty, x1: tx + c.w / 2, y1: ty + c.h } }));
    const free = boxes.filter((s) => !placed.some((p) => hit(p.box, s.box)));
    const onScreen = (b: Box) => !view || (b.x0 >= view.x0 && b.x1 <= view.x1 && b.y0 >= view.y0 && b.y1 <= view.y1);
    const clear = (b: Box) => !order.some((o, i) => o.id !== c.id && hit(discs[i], b));
    // Prefer an on-screen slot that also clears other nodes; near-coincident nodes or the canvas
    // edge may leave none, so then any slot that only clears labels is fine.
    const pick =
      free.find((s) => onScreen(s.box) && clear(s.box)) ??
      free.find((s) => onScreen(s.box)) ??
      free.find((s) => clear(s.box)) ??
      free[0];
    if (pick) placed.push({ id: c.id, ...pick });
  }
  return placed;
}
