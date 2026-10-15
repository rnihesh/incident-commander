/** Value labels on a line chart's points (the learning curve's confidence line), placed so they
 *  clear the line itself, every marker, the other labels and any extra boxes (bar values). */

export interface Pt {
  x: number;
  y: number;
}

export interface Box {
  x0: number;
  y0: number;
  x1: number;
  y1: number;
}

/** One label to place: the point it names, its text box size and the marker radius. */
export interface PointLabel extends Pt {
  w: number;
  h: number;
  r: number;
}

/** Where a label landed: text anchor (`start` / `middle` / `end`), baseline position and its box. */
export interface PlacedPointLabel {
  x: number;
  y: number;
  anchor: "start" | "middle" | "end";
  box: Box;
}

const hit = (a: Box, b: Box) => a.x0 < b.x1 && b.x0 < a.x1 && a.y0 < b.y1 && b.y0 < a.y1;

/** True when the segment p-q passes through box b (sampled; plenty for chart-sized boxes). */
function segmentHits(p: Pt, q: Pt, b: Box): boolean {
  const n = Math.max(8, Math.ceil(Math.hypot(q.x - p.x, q.y - p.y) / 2));
  for (let i = 0; i <= n; i++) {
    const x = p.x + ((q.x - p.x) * i) / n;
    const y = p.y + ((q.y - p.y) * i) / n;
    if (x > b.x0 && x < b.x1 && y > b.y0 && y < b.y1) return true;
  }
  return false;
}

/** Greedy placement, most constrained label first (fewest free slots against the line, markers
 *  and `avoid`). Each label tries above its point, then upper right, upper left, lower right, lower
 *  left, below, then the three upper slots again one line higher; the first slot inside `bounds`
 *  that clears the line (`line`, consecutive points), every marker disc, the labels already placed
 *  and `avoid` wins. Collisions are tested with the text box grown by `pad` (the text halo and the
 *  line's width). With no free slot the least-bad in-bounds slot (fewest collisions) is used.
 *  Results come back in input order. */
export function placePointLabels(labels: PointLabel[], line: Pt[], markers: (Pt & { r: number })[], bounds: Box, avoid: Box[] = [], gap = 4, pad = 3.5): PlacedPointLabel[] {
  const discs: Box[] = markers.map((m) => ({ x0: m.x - m.r, y0: m.y - m.r, x1: m.x + m.r, y1: m.y + m.r }));
  const inBounds = (b: Box) => b.x0 >= bounds.x0 && b.x1 <= bounds.x1 && b.y0 >= bounds.y0 && b.y1 <= bounds.y1;
  const grow = (b: Box): Box => ({ x0: b.x0 - pad, y0: b.y0 - pad, x1: b.x1 + pad, y1: b.y1 + pad });
  const fixedCost = (box: Box) => {
    const b = grow(box);
    let c = 0;
    for (let i = 1; i < line.length; i++) if (segmentHits(line[i - 1], line[i], b)) c++;
    c += discs.filter((d) => hit(d, b)).length;
    c += avoid.filter((a) => hit(a, b)).length;
    return c;
  };
  const slotsFor = (l: PointLabel): PlacedPointLabel[] => {
    const up = l.y - l.r - gap; // baseline above the marker
    const high = up - l.h; // one text line higher
    const down = l.y + l.r + gap + l.h; // baseline below it
    const side = l.r + gap;
    const all = (
      [
        [l.x, up, "middle"],
        [l.x + side, up, "start"],
        [l.x - side, up, "end"],
        [l.x + side, down, "start"],
        [l.x - side, down, "end"],
        [l.x, down, "middle"],
        [l.x, high, "middle"],
        [l.x + side, high, "start"],
        [l.x - side, high, "end"],
      ] as [number, number, PlacedPointLabel["anchor"]][]
    ).map(([x, y, anchor]) => {
      const x0 = anchor === "start" ? x : anchor === "end" ? x - l.w : x - l.w / 2;
      return { x, y, anchor, box: { x0, y0: y - l.h, x1: x0 + l.w, y1: y } };
    });
    const fits = all.filter((s) => inBounds(s.box));
    return fits.length ? fits : all;
  };
  const slots = labels.map(slotsFor);
  const free = slots.map((ss) => ss.filter((s) => fixedCost(s.box) === 0).length);
  const order = labels.map((_, i) => i).sort((a, b) => free[a] - free[b] || a - b);
  const out: PlacedPointLabel[] = new Array(labels.length);
  const placed: Box[] = [];
  for (const i of order) {
    let best = slots[i][0];
    let bestCost = Infinity;
    for (const s of slots[i]) {
      const c = fixedCost(s.box) + placed.filter((p) => hit(p, grow(s.box))).length;
      if (c < bestCost) {
        best = s;
        bestCost = c;
        if (c === 0) break;
      }
    }
    out[i] = best;
    placed.push(best.box);
  }
  return out;
}
