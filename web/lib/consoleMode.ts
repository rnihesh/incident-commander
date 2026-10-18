// What the console shows: the welcome screen, a live run, a recording, or a scripted scenario,
// and when the Inject/Storm/fault controls are locked. Pure so it runs in node tests.
// Nothing plays on its own: a recording starts only when the visitor asks for the sample run,
// or as the fallback when the API refuses a live run.
import type { RunEvent } from "./events";
import type { StreamSource } from "./useRunStream";

export type SessionLike = { runId: string; incidentId: string } | { recorded: true; sample?: boolean; reason?: string } | null;

/** The welcome screen shows until the visitor starts something (or opens a scenario / sample link). */
export function showWelcome(i: { scenario: boolean; session: SessionLike; storm: boolean }): boolean {
  return !i.scenario && !i.session && !i.storm;
}

/** `?sample` asks for the recorded run straight away (the "Watch a sample run" links). */
export function initialSession(i: { sampleParam: string | null; scenarioParam: string | null }): SessionLike {
  return i.sampleParam !== null && i.scenarioParam === null ? { recorded: true, sample: true } : null;
}

/** Recorded playback never locks the controls: pressing Inject takes over. */
export function controlsDisabled(i: {
  scenario: boolean;
  injecting: boolean;
  streamActive: boolean;
  streamRecorded: boolean;
}): boolean {
  return i.scenario || i.injecting || (i.streamActive && !i.streamRecorded);
}

/** The stream source for the console: scenario mock, live run, recording, or idle. */
export function sourceFor(scenario: { events: RunEvent[] } | null, session: SessionLike): StreamSource {
  if (scenario) return { kind: "mock", events: scenario.events };
  if (session && "runId" in session) return { kind: "live", runId: session.runId };
  return { kind: "live", runId: null, recorded: !!session };
}
