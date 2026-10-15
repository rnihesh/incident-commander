import type { RunEvent } from "./events";
import { HERO_EVENTS } from "./mock/hero-run";
import {
  DEGRADED_EVENTS,
  EXPIRED_EVENTS,
  INCONCLUSIVE_EVENTS,
  RATE_LIMITED_EVENTS,
  RECORDED_EVENTS,
  REFUSED_EVENTS,
} from "./mock/fixtures";

export type ScenarioId =
  | "hero"
  | "inconclusive"
  | "refused"
  | "degraded"
  | "recorded"
  | "ratelimited"
  | "expired";

export interface Scenario {
  id: ScenarioId;
  label: string;
  note: string;
  events: RunEvent[];
}

export const SCENARIOS: Scenario[] = [
  { id: "hero", label: "Hero: bad deploy", note: "Full diagnosis, rehearsal, PR, page, write-back.", events: HERO_EVENTS },
  { id: "inconclusive", label: "Inconclusive: rival paths", note: "Two suspects explain everything; no PR.", events: INCONCLUSIVE_EVENTS },
  { id: "refused", label: "Refused: rollback not enough", note: "Rehearsal leaves part of the symptoms explained.", events: REFUSED_EVENTS },
  { id: "degraded", label: "Degraded: fallback verdict", note: "Step cap hit; deterministic funnel concluded.", events: DEGRADED_EVENTS },
  { id: "recorded", label: "Recorded run", note: "Saved stream replayed.", events: RECORDED_EVENTS },
  { id: "ratelimited", label: "Rate limited", note: "Investigation cap reached.", events: RATE_LIMITED_EVENTS },
  { id: "expired", label: "Session expired", note: "Idle session ended.", events: EXPIRED_EVENTS },
];

export function resolveScenario(id: string | null | undefined): Scenario {
  return SCENARIOS.find((s) => s.id === id) ?? SCENARIOS[0];
}
