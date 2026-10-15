import { afterEach, describe, expect, it, vi } from "vitest";
import { NetworkError, createSession, getSessionIncidents, fetchWithTimeout, DEFAULT_TIMEOUT_MS, LONG_TIMEOUT_MS } from "../lib/api";
import { JudgeError, fragilityMap } from "../lib/judge";

const never = () => new Promise<Response>(() => {});

afterEach(() => vi.useRealTimers());

describe("getSessionIncidents", () => {
  it("returns the listed incident ids", async () => {
    const f = async (u: string) => {
      expect(u).toMatch(/\/api\/session\/incidents$/);
      return new Response(JSON.stringify({ incidents: ["inc-1", "inc-2"] }), { status: 200 });
    };
    expect(await getSessionIncidents(f)).toEqual(["inc-1", "inc-2"]);
  });
  it("is empty when the body has no incidents key", async () => {
    expect(await getSessionIncidents(async () => new Response("{}", { status: 200 }))).toEqual([]);
  });
  it("rejects when the request fails", async () => {
    await expect(getSessionIncidents(async () => { throw new Error("boom"); })).rejects.toBeInstanceOf(NetworkError);
    await expect(getSessionIncidents(async () => new Response("{}", { status: 500 }))).rejects.toBeInstanceOf(NetworkError);
  });
});

describe("client timeout", () => {
  it("maps a fetch that never resolves to a NetworkError after the default timeout", async () => {
    vi.useFakeTimers();
    const p = createSession(never);
    const settled = expect(p).rejects.toThrow(/timed out/);
    await vi.advanceTimersByTimeAsync(DEFAULT_TIMEOUT_MS - 1);
    let early = false;
    p.catch(() => { early = true; });
    await Promise.resolve();
    expect(early).toBe(false);
    await vi.advanceTimersByTimeAsync(2);
    await settled;
    await expect(p).rejects.toBeInstanceOf(NetworkError);
  });
  it("gives fragility a longer budget and maps its timeout to JudgeError", async () => {
    vi.useFakeTimers();
    expect(LONG_TIMEOUT_MS).toBeGreaterThan(DEFAULT_TIMEOUT_MS);
    const p = fragilityMap("inc-1", never);
    const settled = expect(p).rejects.toBeInstanceOf(JudgeError);
    await vi.advanceTimersByTimeAsync(DEFAULT_TIMEOUT_MS + 1000);
    let early = false;
    p.catch(() => { early = true; });
    await Promise.resolve();
    expect(early).toBe(false);
    await vi.advanceTimersByTimeAsync(LONG_TIMEOUT_MS);
    await settled;
  });
  it("hands the fetch an abort signal and clears the timer on success", async () => {
    let signal: AbortSignal | undefined;
    const res = await fetchWithTimeout(async (_u, o) => { signal = o?.signal ?? undefined; return new Response("{}"); }, "x", {}, 50);
    expect(res.ok).toBe(true);
    expect(signal?.aborted).toBe(false);
  });
  it("leaves a caller-supplied signal alone (no timeout)", async () => {
    vi.useFakeTimers();
    const ctrl = new AbortController();
    let seen: AbortSignal | null | undefined;
    const slow = (_u: string, o?: RequestInit) => { seen = o?.signal; return new Promise<Response>((r) => setTimeout(() => r(new Response("{}")), 120_000)); };
    const p = fetchWithTimeout(slow, "x", { signal: ctrl.signal }, 50);
    await vi.advanceTimersByTimeAsync(120_001);
    await expect(p).resolves.toBeInstanceOf(Response);
    expect(seen).toBe(ctrl.signal);
  });
});
