// First-paint autopilot (KTD7): decide whether the recorded hero run plays on its own,
// and when the Inject/Storm/fault controls are locked. Pure so it runs in node tests.
import type { RunEvent } from "./events";
import type { StreamSource } from "./useRunStream";

/** sessionStorage key set once the visitor starts anything live. */
export const AUTOPLAY_MARKER = "ic.started";

export interface AutoplayInput {
  /** A session (live or recorded) is already held by the console. */
  hasLiveSession: boolean;
  /** Raw `?scenario=` value, null when absent. */
  scenarioParam: string | null;
  /** The sessionStorage marker is present. */
  marker: boolean;
  /** The server-side session already lists an incident (e.g. opened in a new tab). */
  sessionHasIncident: boolean;
}

export function shouldAutoplay(i: AutoplayInput): boolean {
  return !i.hasLiveSession && i.scenarioParam === null && !i.marker && !i.sessionHasIncident;
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

export type SessionLike = { runId: string; incidentId: string } | { recorded: true } | null;

/** The stream source for the console: scenario mock, live run, recorded fallback, or idle. */
export function sourceFor(scenario: { events: RunEvent[] } | null, session: SessionLike): StreamSource {
  if (scenario) return { kind: "mock", events: scenario.events };
  if (session && "runId" in session) return { kind: "live", runId: session.runId };
  return { kind: "live", runId: null, recorded: !!session };
}

/** Re-check when the session-incidents fetch settles: a click, a held session or a new marker beats autoplay. */
export function shouldApplyAutoplay(i: AutoplayInput & { cancelled: boolean; userActed: boolean }): boolean {
  if (i.cancelled || i.userActed) return false;
  return shouldAutoplay(i);
}
