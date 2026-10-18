import { describe, expect, it } from "vitest";
import { limitNotice } from "../lib/limits";

describe("limitNotice", () => {
  it("turns the per-client session cap into one friendly message", () => {
    const n = limitNotice("at most 2 concurrent sessions per client");
    expect(n?.body).toBe(
      "You already have 2 live sandboxes open in other tabs or browsers. They expire after 30 minutes idle; meanwhile you can watch a sample run.",
    );
    expect(n?.detail).not.toContain("sample run");
    expect(n?.body).not.toContain("concurrent sessions");
  });

  it("covers capacity, hourly, spend-cap and busy refusals", () => {
    expect(limitNotice("server is at capacity (8 concurrent sessions)")?.title).toBe("The demo is busy");
    expect(limitNotice("at most 6 new sessions per hour per client")?.detail).toContain("6 new sandboxes");
    expect(limitNotice("daily LLM spend cap reached; serving recorded runs")?.title).toBe("Live runs are paused for today");
    expect(limitNotice("a run is already in progress for this session")?.body).not.toContain("sample run");
  });

  it("falls back to a generic limit for other per-period caps", () => {
    expect(limitNotice("at most 3 investigations per hour per session")?.title).toBe("Limit reached");
    expect(limitNotice("limit of 3 runs per hour per session reached")?.detail).toContain("started 3 live investigations");
    expect(limitNotice("global hourly run ceiling reached")?.title).toBe("The demo is busy");
  });

  it("returns null for anything that is not a limit", () => {
    expect(limitNotice(null)).toBeNull();
    expect(limitNotice("")).toBeNull();
    expect(limitNotice("unknown service svc-x")).toBeNull();
  });
});
