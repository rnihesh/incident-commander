import { SCHEMA_VERSION, type AgentName, type EventType, type RunEvent } from "../events";

/** Deterministic 64-hex stand-in for sha256. Fixtures only; never used for verification. */
export function fakeHash(input: string): string {
  let h1 = 0xdeadbeef ^ input.length;
  let h2 = 0x41c6ce57;
  const out: string[] = [];
  for (let round = 0; round < 8; round++) {
    for (let i = 0; i < input.length; i++) {
      const c = input.charCodeAt(i) + round * 131;
      h1 = Math.imul(h1 ^ c, 2654435761);
      h2 = Math.imul(h2 ^ c, 1597334677);
    }
    h1 = Math.imul(h1 ^ (h1 >>> 16), 2246822507) ^ Math.imul(h2 ^ (h2 >>> 13), 3266489909);
    h2 = Math.imul(h2 ^ (h2 >>> 16), 2246822507) ^ Math.imul(h1 ^ (h1 >>> 13), 3266489909);
    out.push((h1 >>> 0).toString(16).padStart(8, "0"));
  }
  return out.join("");
}

export class EventBuilder {
  private id = 0;
  readonly events: RunEvent[] = [];
  constructor(
    private runId: string,
    private t0: number,
  ) {}
  add<T extends object>(type: EventType, agent: AgentName, offsetMs: number, data: T): this {
    this.events.push({
      id: this.id++,
      run_id: this.runId,
      ts: this.t0 + offsetMs,
      type,
      agent,
      data: data as Record<string, unknown>,
      schema_version: SCHEMA_VERSION,
    });
    return this;
  }
}

export const T0 = 1791554801000; // 2026-10-09T12:06:41Z, frozen world clock
