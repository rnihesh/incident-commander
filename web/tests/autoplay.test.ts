import { describe, expect, it } from "vitest";
import { AUTOPLAY_MARKER, controlsDisabled, shouldApplyAutoplay, shouldAutoplay, sourceFor } from "../lib/autoplay";

const fresh = { hasLiveSession: false, scenarioParam: null, marker: false, sessionHasIncident: false };

describe("shouldAutoplay (AE6)", () => {
  it("plays on a first visit: no session, no scenario, no marker, no incident", () => {
    expect(shouldAutoplay(fresh)).toBe(true);
  });
  it("does not play over a scenario param (even an empty one)", () => {
    expect(shouldAutoplay({ ...fresh, scenarioParam: "hero" })).toBe(false);
    expect(shouldAutoplay({ ...fresh, scenarioParam: "" })).toBe(false);
  });
  it("does not play once the marker is set", () => {
    expect(shouldAutoplay({ ...fresh, marker: true })).toBe(false);
  });
  it("does not play when the session already has an incident, even in a new tab", () => {
    expect(shouldAutoplay({ ...fresh, sessionHasIncident: true })).toBe(false);
  });
  it("does not play when a session is already held", () => {
    expect(shouldAutoplay({ ...fresh, hasLiveSession: true })).toBe(false);
  });
  it("exposes a stable marker key", () => {
    expect(AUTOPLAY_MARKER).toBe("ic.started");
  });
});

describe("controlsDisabled", () => {
  const idle = { scenario: false, injecting: false, streamActive: false, streamRecorded: false };
  it("stays enabled while a recorded stream plays", () => {
    expect(controlsDisabled({ ...idle, streamActive: true, streamRecorded: true })).toBe(false);
  });
  it("is disabled while a live run is active", () => {
    expect(controlsDisabled({ ...idle, streamActive: true })).toBe(true);
  });
  it("is disabled for scenarios and while injecting", () => {
    expect(controlsDisabled({ ...idle, scenario: true })).toBe(true);
    expect(controlsDisabled({ ...idle, injecting: true })).toBe(true);
  });
  it("is enabled when idle", () => {
    expect(controlsDisabled(idle)).toBe(false);
  });
});

describe("stream source on takeover", () => {
  it("switches from recorded to live when inject yields a run", () => {
    expect(sourceFor(null, { recorded: true })).toEqual({ kind: "live", runId: null, recorded: true });
    expect(sourceFor(null, { runId: "run-1", incidentId: "inc-1" })).toEqual({ kind: "live", runId: "run-1" });
  });
  it("a scenario always wins with the mock source", () => {
    const events = [] as never[];
    expect(sourceFor({ events }, { runId: "r", incidentId: "i" })).toEqual({ kind: "mock", events });
  });
  it("no session means nothing started", () => {
    expect(sourceFor(null, null)).toEqual({ kind: "live", runId: null, recorded: false });
  });
});

describe("shouldApplyAutoplay (late session-incidents fetch)", () => {
  const settled = { ...fresh, cancelled: false, userActed: false };
  it("plays when nothing happened while the fetch was pending", () => {
    expect(shouldApplyAutoplay(settled)).toBe(true);
  });
  it("user acted while the session-incidents fetch was pending: no autoplay", () => {
    expect(shouldApplyAutoplay({ ...settled, userActed: true })).toBe(false);
  });
  it("cancelled effects and listed incidents never play", () => {
    expect(shouldApplyAutoplay({ ...settled, cancelled: true })).toBe(false);
    expect(shouldApplyAutoplay({ ...settled, sessionHasIncident: true })).toBe(false);
  });
});
