import { describe, expect, it } from "vitest";
import { labelRank, placeLabels, type LabelCandidate } from "../lib/labels";
import type { GraphView } from "../lib/reducer";

const cand = (id: string, x: number, y: number, rank: number, w = 40): LabelCandidate => ({ id, x, y, r: 4, w, h: 10, rank });

describe("placeLabels", () => {
  it("puts a lone label centred under its node", () => {
    const [p] = placeLabels([cand("a", 0, 0, 2)], 3);
    expect(p).toMatchObject({ id: "a", tx: 0, ty: 7 });
  });

  it("keeps a label inside the visible canvas when its default slot would run off the edge", () => {
    // Node near the bottom edge: below would leave the view, so the label goes above it.
    const view = { x0: -100, y0: -100, x1: 100, y1: 10 };
    const [p] = placeLabels([cand("edge", 0, 0, 2)], 3, view);
    expect(p.box.y1).toBeLessThanOrEqual(view.y1);
    expect(p.ty).toBeLessThan(0);
    // A node at the right edge whose below and above slots are taken flips its label left, not right.
    const placed = placeLabels([cand("a", 90, 20, 0), cand("b", 90, -20, 1), cand("c", 90, 0, 2)], 3, { x0: -200, y0: -200, x1: 120, y1: 200 });
    const c = placed.find((x) => x.id === "c")!;
    expect(c.box.x1).toBeLessThanOrEqual(120);
  });

  it("moves the lower-priority label of two near-coincident nodes off the first", () => {
    const placed = placeLabels([cand("symptom", 2, 1, 3), cand("alert", 0, 0, 0)], 3);
    expect(placed.map((p) => p.id)).toEqual(["alert", "symptom"]);
    const [a, b] = placed;
    expect(a.ty).toBe(7); // the alerting node keeps the default slot
    const overlap = a.box.x0 < b.box.x1 && b.box.x0 < a.box.x1 && a.box.y0 < b.box.y1 && b.box.y0 < a.box.y1;
    expect(overlap).toBe(false);
  });

  it("skips a label when every slot is taken", () => {
    const pile = Array.from({ length: 6 }, (_, i) => cand(`n${i}`, i * 0.5, 0, i));
    const placed = placeLabels(pile, 3);
    expect(placed.length).toBeLessThan(6);
    expect(placed[0].id).toBe("n0");
    for (let i = 0; i < placed.length; i++)
      for (let j = i + 1; j < placed.length; j++) {
        const [a, b] = [placed[i].box, placed[j].box];
        expect(a.x0 < b.x1 && b.x0 < a.x1 && a.y0 < b.y1 && b.y0 < a.y1).toBe(false);
      }
  });
});

describe("labelRank", () => {
  const g = { suspects: ["root"] } as unknown as GraphView;
  it("orders alerting, suspect, path, symptomatic, the rest", () => {
    expect(labelRank("x", "alerting", g)).toBe(0);
    expect(labelRank("root", "path", g)).toBe(1);
    expect(labelRank("hop", "path", g)).toBe(2);
    expect(labelRank("s", "symptomatic", g)).toBe(3);
    expect(labelRank("p", "plain", g)).toBe(4);
  });
});
