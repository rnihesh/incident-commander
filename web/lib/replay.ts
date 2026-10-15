// Pure logic for the replay scrubber: a frame is only a set of highlights over a fixed
// layout, so everything here maps (response, frame index) to canvas inputs.
import type { GraphView } from "./reducer";
import type { IncidentGraphResponse } from "./api";

export type ReplayViewName = "true" | "known";

export interface ReplayFrame {
  t: number;
  changes_active: string[];
  symptomatic: string[];
  late_hidden: string[];
}
export interface ReplayChange {
  id: string;
  service_id: string;
  kind: string;
  valid_from: number;
  recorded_at: number | null;
  late: boolean;
}
export interface ReplayMarkers {
  root_event_t: number | null;
  verdict_suspect_t: number | null;
  alert_t: number;
  earliest_onset_t: number | null;
}
export interface ReplayResponse {
  incident_id: string;
  view: ReplayViewName;
  alert_service_id: string | null;
  nodes: IncidentGraphResponse["nodes"];
  edges: IncidentGraphResponse["edges"];
  changes: ReplayChange[];
  frames: ReplayFrame[];
  markers: ReplayMarkers;
}

export const REPLAY_SPEEDS = [1, 2, 4] as const;
export type ReplaySpeed = (typeof REPLAY_SPEEDS)[number];
/** Wall-clock ms per frame at 1x. */
export const BASE_FRAME_MS = 400;

export const clampIndex = (i: number, n: number) => (n <= 0 ? 0 : Math.min(Math.max(Math.round(i), 0), n - 1));

/** The frame index a key press moves to, or null when the key is not a scrubber key. */
export function keyIndex(key: string, i: number, n: number, shift = false): number | null {
  const big = shift ? 10 : 1;
  switch (key) {
    case "ArrowRight":
    case "ArrowUp":
      return clampIndex(i + big, n);
    case "ArrowLeft":
    case "ArrowDown":
      return clampIndex(i - big, n);
    case "PageUp":
      return clampIndex(i + 10, n);
    case "PageDown":
      return clampIndex(i - 10, n);
    case "Home":
      return 0;
    case "End":
      return clampIndex(n - 1, n);
    default:
      return null;
  }
}

/** Index of the frame closest in time to t (used to keep the position across the view toggle). */
export function nearestFrame(frames: ReplayFrame[], t: number): number {
  let best = 0;
  for (let i = 1; i < frames.length; i++) if (Math.abs(frames[i].t - t) < Math.abs(frames[best].t - t)) best = i;
  return best;
}

/** Position of a time on the scrubber, 0..100 (clamped). */
export function tickPercent(t: number, first: number, last: number): number {
  if (last <= first) return 0;
  return Math.min(100, Math.max(0, ((t - first) / (last - first)) * 100));
}

export interface Tick {
  key: "verdict" | "onset" | "alert";
  label: string;
  t: number;
  pct: number;
}

export function ticksFor(markers: ReplayMarkers, frames: ReplayFrame[]): Tick[] {
  if (!frames.length) return [];
  const first = frames[0].t;
  const last = frames[frames.length - 1].t;
  const out: Tick[] = [];
  const add = (key: Tick["key"], label: string, t: number | null) => {
    if (t !== null && t !== undefined) out.push({ key, label, t, pct: tickPercent(t, first, last) });
  };
  add("verdict", "Agent's suspect", markers.verdict_suspect_t);
  add("onset", "Earliest onset", markers.earliest_onset_t);
  add("alert", "Alert", markers.alert_t);
  return out;
}

/** Canvas highlights for one frame: services with a landed change are suspect-ringed, services
 * past their onset are symptomatic. No late/hidden roles: late items are badged in the list. */
export function frameGraphView(resp: ReplayResponse, frame: ReplayFrame): GraphView {
  const serviceOf = new Map(resp.changes.map((c) => [c.id, c.service_id]));
  const suspects = [...new Set(frame.changes_active.map((c) => serviceOf.get(c)).filter((x): x is string => !!x))];
  return {
    alerting: resp.alert_service_id ?? resp.nodes.find((n) => n.alerting)?.id ?? null,
    symptomatic: frame.symptomatic,
    suspects,
    eliminated: {},
    paths: [],
    late: [],
    falsified: [],
  };
}

/** Changes landed at this frame, newest last, and which are late-arriving (true view only). */
export function landedChanges(resp: ReplayResponse, frame: ReplayFrame): (ReplayChange & { badge: boolean })[] {
  const active = new Set(frame.changes_active);
  const late = new Set(frame.late_hidden);
  return resp.changes.filter((c) => active.has(c.id)).map((c) => ({ ...c, badge: late.has(c.id) }));
}

export const fmtClock = (t: number) => new Date(t * 1000).toISOString().slice(11, 19) + "Z";

/** Signed offset from the alert, e.g. "T-12m05s". */
export function fmtOffset(t: number, alert: number): string {
  const d = t - alert;
  const sign = d < 0 ? "-" : d > 0 ? "+" : "";
  const a = Math.abs(d);
  const m = Math.floor(a / 60);
  const s = a % 60;
  return m ? `T${sign}${m}m${String(s).padStart(2, "0")}s` : `T${sign}${s}s`;
}
