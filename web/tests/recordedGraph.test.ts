import { describe, expect, it } from "vitest";
import { getRecordedHeroGraph, NetworkError, type IncidentGraphResponse } from "../lib/api";
import { pickGraph } from "../lib/liveGraph";

const resp = (incident_id: string): IncidentGraphResponse => ({
  incident_id, t: 1, alert_service_id: "a", nodes: [], edges: [], changes: [],
});
const LIVE = resp("inc-live");
const REC = resp("inc-041");

describe("pickGraph", () => {
  it("draws the fetched incident graph for a live run", () => {
    expect(pickGraph({ kind: "live", incidentId: "inc-live", live: LIVE, recorded: REC })).toEqual({ resp: LIVE, pending: false });
    expect(pickGraph({ kind: "live", incidentId: "inc-live", live: null, recorded: REC })).toEqual({ resp: null, pending: false });
  });
  it("draws the graph saved with the recording during recorded playback", () => {
    expect(pickGraph({ kind: "recorded", incidentId: "inc-041", live: LIVE, recorded: REC })).toEqual({ resp: REC, pending: false });
    // before run_started arrives there is nothing to match yet; still use the recording
    expect(pickGraph({ kind: "recorded", incidentId: null, live: null, recorded: REC }).resp).toBe(REC);
  });
  it("waits for the recorded graph, then falls back to the mock hero graph if it failed", () => {
    expect(pickGraph({ kind: "recorded", incidentId: "inc-041", live: null, recorded: undefined })).toEqual({ resp: null, pending: true });
    expect(pickGraph({ kind: "recorded", incidentId: "inc-041", live: null, recorded: null })).toEqual({ resp: null, pending: false });
  });
  it("ignores a recorded graph for a different incident than the one playing", () => {
    expect(pickGraph({ kind: "recorded", incidentId: "inc-007", live: null, recorded: REC })).toEqual({ resp: null, pending: false });
  });
  it("keeps the mock hero graph for scripted scenarios", () => {
    expect(pickGraph({ kind: "mock", incidentId: "inc-041", live: LIVE, recorded: REC })).toEqual({ resp: null, pending: false });
  });
});

describe("getRecordedHeroGraph", () => {
  it("fetches the saved graph", async () => {
    const urls: string[] = [];
    const f = async (u: string) => {
      urls.push(String(u));
      return new Response(JSON.stringify(REC), { status: 200 });
    };
    expect(await getRecordedHeroGraph(f)).toEqual(REC);
    expect(urls[0]).toMatch(/\/api\/recorded\/hero\/graph$/);
  });
  it("rejects on 404 so the console can fall back", async () => {
    const f = async () => new Response(JSON.stringify({ detail: "no recorded graph available" }), { status: 404 });
    await expect(getRecordedHeroGraph(f)).rejects.toBeInstanceOf(NetworkError);
  });
});
