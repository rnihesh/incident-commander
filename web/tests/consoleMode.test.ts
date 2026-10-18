import { describe, expect, it } from "vitest";
import { controlsDisabled, initialSession, showWelcome, sourceFor } from "../lib/consoleMode";

describe("showWelcome", () => {
  it("shows on a first visit: no scenario, no session, no storm", () => {
    expect(showWelcome({ scenario: false, session: null, storm: false })).toBe(true);
  });
  it("hides once anything starts", () => {
    expect(showWelcome({ scenario: true, session: null, storm: false })).toBe(false);
    expect(showWelcome({ scenario: false, session: { recorded: true, sample: true }, storm: false })).toBe(false);
    expect(showWelcome({ scenario: false, session: { runId: "r", incidentId: "i" }, storm: false })).toBe(false);
    expect(showWelcome({ scenario: false, session: null, storm: true })).toBe(false);
  });
});

describe("initialSession", () => {
  it("never plays a recording on its own", () => {
    expect(initialSession({ sampleParam: null, scenarioParam: null })).toBeNull();
  });
  it("plays the sample run only when asked with ?sample", () => {
    expect(initialSession({ sampleParam: "1", scenarioParam: null })).toEqual({ recorded: true, sample: true });
    expect(initialSession({ sampleParam: "", scenarioParam: null })).toEqual({ recorded: true, sample: true });
  });
  it("a scenario param wins over ?sample", () => {
    expect(initialSession({ sampleParam: "1", scenarioParam: "hero" })).toBeNull();
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
