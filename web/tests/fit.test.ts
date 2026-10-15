import { describe, expect, it } from "vitest";
import { labelAwareFit } from "../lib/fit";

const OPTS = { pad: 20, r: 10, lh: 16, kMax: 4 };

describe("labelAwareFit", () => {
  it("keeps an edge node's label inside a narrow canvas", () => {
    // Two labelled nodes 300 graph units apart, 130px labels, on a 360px-wide phone canvas.
    const pts = [
      { x: 0, y: 0, lw: 130 },
      { x: 300, y: 0, lw: 130 },
    ];
    const f = labelAwareFit(pts, 360, 400, OPTS)!;
    // Screen x of each label edge, with the fitted centre in the middle of the canvas.
    const sx = (x: number) => 180 + (x - f.cx) * f.k;
    expect(sx(0) - 65).toBeGreaterThanOrEqual(20 - 1e-6);
    expect(sx(300) + 65).toBeLessThanOrEqual(340 + 1e-6);
    // A plain node fit (labels ignored) would have zoomed further in and clipped them.
    expect(f.k).toBeLessThan((360 - 40) / 300);
  });

  it("caps the zoom for a single node and returns null when labels alone overflow", () => {
    expect(labelAwareFit([{ x: 5, y: 5, lw: 40 }], 400, 300, OPTS)).toMatchObject({ k: 4, cx: 5 });
    expect(labelAwareFit([{ x: 0, y: 0, lw: 500 }], 300, 300, OPTS)).toBeNull();
    expect(labelAwareFit([], 300, 300, OPTS)).toBeNull();
  });
});
