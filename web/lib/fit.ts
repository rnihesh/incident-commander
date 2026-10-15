/** Label-aware zoom-to-fit for the graph canvas. force-graph's zoomToFit frames node centres
 *  only, so on a narrow canvas the labels of edge nodes ran off the side. Labels are drawn at a
 *  fixed screen size (11px under the node), so the fit solves for the largest zoom at which every
 *  node and its label stay inside the padded box. */

export interface FitPoint {
  x: number;
  y: number;
  /** Width of the node's label in screen pixels; 0 when the node is drawn without one. */
  lw: number;
}

export interface FitOpts {
  /** Screen pixels kept clear on every side. */
  pad: number;
  /** Node radius allowance in screen pixels. */
  r: number;
  /** Label line height in screen pixels (the label sits under its node). */
  lh: number;
  /** Highest zoom to return (a single node would otherwise zoom without bound). */
  kMax: number;
}

/** Screen-space extent of the points at zoom k: [left, right, top, bottom]. */
function extent(pts: FitPoint[], k: number, o: FitOpts): [number, number, number, number] {
  let l = Infinity;
  let r = -Infinity;
  let t = Infinity;
  let b = -Infinity;
  for (const p of pts) {
    const half = Math.max(o.r, p.lw / 2);
    l = Math.min(l, p.x * k - half);
    r = Math.max(r, p.x * k + half);
    t = Math.min(t, p.y * k - o.r);
    b = Math.max(b, p.y * k + (p.lw ? o.r + o.lh : o.r));
  }
  return [l, r, t, b];
}

/** Zoom and graph-space centre that fit every point and label inside a w x h canvas, or null
 *  when there are no points or the labels alone are wider than the canvas (the caller then
 *  falls back to a plain node fit). */
export function labelAwareFit(pts: FitPoint[], w: number, h: number, o: FitOpts): { k: number; cx: number; cy: number } | null {
  if (!pts.length) return null;
  const aw = w - 2 * o.pad;
  const ah = h - 2 * o.pad;
  const fits = (k: number) => {
    const [l, r, t, b] = extent(pts, k, o);
    return r - l <= aw && b - t <= ah;
  };
  let lo = 1e-4;
  if (!fits(lo)) return null;
  let hi = o.kMax;
  if (fits(hi)) lo = hi;
  else
    for (let i = 0; i < 40; i++) {
      const mid = (lo + hi) / 2;
      if (fits(mid)) lo = mid;
      else hi = mid;
    }
  const [l, r, t, b] = extent(pts, lo, o);
  return { k: lo, cx: (l + r) / 2 / lo, cy: (t + b) / 2 / lo };
}
