import { beforeAll, describe, expect, it } from "vitest";
import { streamRunEvents } from "../lib/sse";
import { SCHEMA_VERSION, type RunEvent } from "../lib/events";
import { reduceAll } from "../lib/reducer";

beforeAll(() => {
  // fetch-event-source schedules retries via window.setTimeout.
  const g = globalThis as Record<string, unknown>;
  g.window = globalThis;
  g.document = { addEventListener() {}, removeEventListener() {}, hidden: false };
});

const ev = (id: number, type: RunEvent["type"] = "narration"): RunEvent => ({
  id, run_id: "r1", ts: id * 10, type, agent: "system", data: type === "done" ? { ms: 1 } : { text: `n${id}` }, schema_version: SCHEMA_VERSION,
});
const frame = (e: RunEvent) => `id: ${e.id}\ndata: ${JSON.stringify(e)}\n\n`;
const sse = (body: string) =>
  new Response(new TextEncoder().encode(body), { status: 200, headers: { "content-type": "text/event-stream" } });

function fakeFetch(responses: (() => Response)[]) {
  const calls: { url: string; headers: Record<string, string> }[] = [];
  const f = async (url: string, init?: RequestInit) => {
    calls.push({ url, headers: { ...(init?.headers as Record<string, string>) } });
    const next = responses.shift();
    if (!next) throw new Error("no more responses");
    return next();
  };
  return { f, calls };
}

describe("streamRunEvents", () => {
  it("parses frames, skips malformed ones, stops at done", async () => {
    const { f, calls } = fakeFetch([() => sse(frame(ev(0)) + "data: {not json\n\n" + frame(ev(1)) + frame(ev(2, "done")))]);
    const got: RunEvent[] = [];
    await streamRunEvents("r1", { fetch: f, baseUrl: "http://x", onEvent: (e) => got.push(e) });
    expect(got.map((e) => e.id)).toEqual([0, 1, 2]);
    expect(calls).toHaveLength(1);
    expect(calls[0].url).toBe("http://x/api/runs/r1/events");
  });

  it("reconnects with Last-Event-ID after an early close; duplicates are ignored", async () => {
    const { f, calls } = fakeFetch([
      () => sse(frame(ev(0)) + frame(ev(1))),
      () => sse(frame(ev(1)) + frame(ev(2)) + frame(ev(3, "done"))),
    ]);
    const got: RunEvent[] = [];
    await streamRunEvents("r1", { fetch: f, baseUrl: "http://x", retryMs: 1, onEvent: (e) => got.push(e) });
    expect(calls).toHaveLength(2);
    const hdr = (i: number) => Object.fromEntries(Object.entries(calls[i].headers).map(([k, v]) => [k.toLowerCase(), v]));
    expect(hdr(0)["last-event-id"]).toBeUndefined();
    expect(hdr(1)["last-event-id"]).toBe("1");
    expect(got.map((e) => e.id)).toEqual([0, 1, 2, 3]);
    // Even a replay straight into the reducer is idempotent.
    const twice = reduceAll([...got, ...got]);
    expect(twice.narration).toEqual(["n0", "n1", "n2", "n3"].slice(0, 3));
    expect(twice.done).toBe(true);
  });

  it("raises a FallbackError when the stream is refused with 503", async () => {
    const { f } = fakeFetch([() => new Response("{}", { status: 503 })]);
    const e = await streamRunEvents("r1", { fetch: f, baseUrl: "http://x", onEvent: () => {} }).catch((x) => x);
    expect(e.name).toBe("FallbackError");
  });

  it("aborts cleanly", async () => {
    const ctrl = new AbortController();
    const { f } = fakeFetch([() => sse(frame(ev(0)))]);
    ctrl.abort();
    await expect(streamRunEvents("r1", { fetch: f, signal: ctrl.signal, onEvent: () => {} })).resolves.toBeUndefined();
  });
});
