import { describe, expect, it } from "vitest";
import { placePointLabels, type Box, type Pt } from "../lib/chartLabels";

const hit = (a: Box, b: Box) => a.x0 < b.x1 && b.x0 < a.x1 && a.y0 < b.y1 && b.y0 < a.y1;
function crosses(p: Pt, q: Pt, b: Box) {
  for (let i = 0; i <= 200; i++) {
    const x = p.x + ((q.x - p.x) * i) / 200;
    const y = p.y + ((q.y - p.y) * i) / 200;
    if (x > b.x0 && x < b.x1 && y > b.y0 && y < b.y1) return true;
  }
  return false;
}

describe("placePointLabels", () => {
  const bounds = { x0: 0, y0: 0, x1: 520, y1: 117 };

  it("puts a lone peak label straight above its marker", () => {
    const line = [{ x: 0, y: 60 }, { x: 100, y: 20 }, { x: 200, y: 60 }];
    const [p] = placePointLabels([{ x: 100, y: 20, r: 3, w: 24, h: 9 }], line, line.map((q) => ({ ...q, r: 3 })), bounds);
    expect(p.anchor).toBe("middle");
    expect(p.box.y1).toBeLessThan(20);
  });

  it("keeps a falling tail's labels (the learning curve's ÷2 points) off the line, the markers and each other", () => {
    // The published forgetting run: peak 0.99, then three halvings, plotted as in LearningCurveChart.
    const cy = (c: number) => 118 - c * 98;
    const pts = [0.6, 0.8, 0.9, 0.95, 0.97, 0.99, 0.495, 0.2475, 0.124].map((c, i) => ({ x: 36 + 56 * i, y: cy(c), r: 4.25 }));
    const want = [5, 6, 7, 8];
    const placed = placePointLabels(
      want.map((i) => ({ x: pts[i].x, y: pts[i].y, r: pts[i].r, w: (i === 5 ? 4 : 7) * 6.1, h: 9 })),
      pts,
      pts,
      bounds,
      [{ x0: 8, y0: cy(0.25) - 0.5, x1: 512, y1: cy(0.25) + 0.5 }],
    );
    placed.forEach((p, k) => {
      expect(p.box.x0).toBeGreaterThanOrEqual(0);
      expect(p.box.x1).toBeLessThanOrEqual(520);
      for (let i = 1; i < pts.length; i++) expect(crosses(pts[i - 1], pts[i], p.box)).toBe(false);
      for (const m of pts) expect(hit(p.box, { x0: m.x - m.r, y0: m.y - m.r, x1: m.x + m.r, y1: m.y + m.r })).toBe(false);
      placed.forEach((o, j) => j !== k && expect(hit(p.box, o.box)).toBe(false));
    });
  });
});
