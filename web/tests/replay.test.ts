import { describe, expect, it } from "vitest";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import {
  clampIndex,
  fmtOffset,
  frameGraphView,
  keyIndex,
  landedChanges,
  nearestFrame,
  ticksFor,
  type ReplayFrame,
  type ReplayResponse,
} from "../lib/replay";
import { getIncidentReplay } from "../lib/api";
import { Scrubber } from "../components/Scrubber";

const node = (id: string, onset: number | null = null, alerting = false) => ({
  id, name: id, region: "r", tier: 1, x: 0, y: 0, symptomatic: onset !== null, onset, signature: null, alerting,
});
const frames: ReplayFrame[] = [
  { t: 100, changes_active: [], symptomatic: [], late_hidden: ["c-late"] },
  { t: 200, changes_active: ["c1"], symptomatic: [], late_hidden: ["c-late"] },
  { t: 300, changes_active: ["c1", "c-late"], symptomatic: ["a"], late_hidden: ["c-late"] },
  { t: 400, changes_active: ["c1", "c-late"], symptomatic: ["a", "b"], late_hidden: ["c-late"] },
];
const resp: ReplayResponse = {
  incident_id: "i1",
  view: "true",
  alert_service_id: "b",
  nodes: [node("a", 250), node("b", 350, true), node("c")],
  edges: [{ source: "a", target: "b" }],
  changes: [
    { id: "c1", service_id: "a", kind: "deploy", valid_from: 150, recorded_at: 150, late: false },
    { id: "c-late", service_id: "c", kind: "config", valid_from: 250, recorded_at: 999, late: true },
  ],
  frames,
  markers: { root_event_t: null, verdict_suspect_t: 150, alert_t: 370, earliest_onset_t: 250 },
};

describe("frame to highlight mapping", () => {
  it("maps symptomatic services and landed changes without re-layout inputs", () => {
    expect(frameGraphView(resp, frames[0])).toMatchObject({ symptomatic: [], suspects: [], alerting: "b", late: [] });
    expect(frameGraphView(resp, frames[1]).suspects).toEqual(["a"]);
    const g = frameGraphView(resp, frames[3]);
    expect(g.symptomatic).toEqual(["a", "b"]); // onset order preserved from the frame
    expect(g.suspects).toEqual(["a", "c"]);
  });
  it("badges late-arriving changes only where the frame lists them as late_hidden", () => {
    const asHappened = landedChanges(resp, frames[2]);
    expect(asHappened.map((c) => [c.id, c.badge])).toEqual([["c1", false], ["c-late", true]]);
    const known = { ...resp, view: "known" as const, changes: resp.changes.filter((c) => !c.late) };
    const knownFrame = { ...frames[2], changes_active: ["c1"], late_hidden: [] };
    expect(landedChanges(known, knownFrame).map((c) => c.id)).toEqual(["c1"]);
  });
});

describe("view toggle", () => {
  it("keeps the moment by snapping to the nearest frame of the other view", () => {
    const other: ReplayFrame[] = [{ ...frames[0], t: 90 }, { ...frames[1], t: 210 }, { ...frames[2], t: 330 }];
    expect(nearestFrame(other, 200)).toBe(1);
    expect(nearestFrame(other, 1000)).toBe(2);
  });
  it("requests the chosen view from the API", async () => {
    let url = "";
    const f = async (u: string) => ((url = u), new Response(JSON.stringify(resp), { status: 200 }));
    await getIncidentReplay("i1", "known", f);
    expect(url).toMatch(/\/api\/incidents\/i1\/replay\?view=known$/);
  });
});

describe("keyboard stepping", () => {
  it("steps one frame with arrows, ten with shift, and jumps with Home/End", () => {
    expect(keyIndex("ArrowRight", 1, 4)).toBe(2);
    expect(keyIndex("ArrowLeft", 1, 4)).toBe(0);
    expect(keyIndex("ArrowLeft", 0, 4)).toBe(0);
    expect(keyIndex("ArrowRight", 3, 4)).toBe(3);
    expect(keyIndex("ArrowRight", 0, 50, true)).toBe(10);
    expect(keyIndex("Home", 2, 4)).toBe(0);
    expect(keyIndex("End", 0, 4)).toBe(3);
    expect(keyIndex("a", 1, 4)).toBeNull();
    expect(clampIndex(9, 4)).toBe(3);
  });
});

describe("ticks and labels", () => {
  it("places verdict, onset and alert ticks; omits an absent verdict", () => {
    const t = ticksFor(resp.markers, frames);
    expect(t.map((x) => x.key)).toEqual(["verdict", "onset", "alert"]);
    expect(t.every((x) => x.pct >= 0 && x.pct <= 100)).toBe(true);
    expect(ticksFor({ ...resp.markers, verdict_suspect_t: null }, frames).map((x) => x.key)).toEqual(["onset", "alert"]);
  });
  it("formats offsets from the alert", () => {
    expect(fmtOffset(370, 370)).toBe("T0s");
    expect(fmtOffset(310, 370)).toBe("T-1m00s");
    expect(fmtOffset(375, 370)).toBe("T+5s");
  });
  it("renders an accessible slider, speeds and tick marks", () => {
    const html = renderToStaticMarkup(
      createElement(Scrubber, {
        frames, markers: resp.markers, index: 1, onIndex: () => {}, playing: false, onPlayingChange: () => {}, speed: 2, onSpeed: () => {},
      }),
    );
    expect(html).toContain('aria-label="Replay position"');
    expect(html).toContain('type="range"');
    expect(html).toContain("Agent&#x27;s suspect");
    expect(html).toMatch(/aria-checked="true"[^>]*>2x/);
    expect(html).toContain("Play");
  });
});
