import { describe, expect, it } from "vitest";
import { FallbackError, NetworkError, RateLimitedError, RetryableError, createSession, exonerate, getIncidentReplay, getRecordedHero, injectFault, verifyIncident } from "../lib/api";
import { mapVerify } from "../lib/verify";
import { parseExonerate } from "../lib/exonerate";
import { deriveBanners, reduceAll } from "../lib/reducer";
import { HERO_EVENTS } from "../lib/mock/hero-run";

const json = (status: number, body: unknown) => async () =>
  new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });

describe("api errors", () => {
  it("maps 429 to RateLimitedError (a FallbackError)", async () => {
    const e = await injectFault("bad_deploy", undefined, json(429, { fallback: "recorded_run", reason: "cap" })).catch((x) => x);
    expect(e).toBeInstanceOf(RateLimitedError);
    expect(e).toBeInstanceOf(FallbackError);
    expect(e.reason).toBe("cap");
  });
  it("maps 503 to FallbackError", async () => {
    const e = await injectFault("flag_flip", 1, json(503, { fallback: "recorded_run", reason: "breaker" })).catch((x) => x);
    expect(e).toBeInstanceOf(FallbackError);
    expect(e).not.toBeInstanceOf(RateLimitedError);
  });
  it("maps transport failure and other statuses to NetworkError", async () => {
    const down = async () => { throw new TypeError("fetch failed"); };
    expect(await getRecordedHero(down).catch((x) => x)).toBeInstanceOf(NetworkError);
    expect(await verifyIncident("i1", json(500, {})).catch((x) => x)).toBeInstanceOf(NetworkError);
  });
  it("sends credentials, JSON body and content type", async () => {
    let init: RequestInit | undefined;
    let url = "";
    await injectFault("config_change", 7, async (u, i) => {
      url = u; init = i;
      return new Response(JSON.stringify({ run_id: "r", incident_id: "i", alert_service_id: "s", alert_time: 1, degraded: false }), { status: 200 });
    });
    expect(url).toMatch(/\/api\/inject$/);
    expect(init?.credentials).toBe("include");
    expect(JSON.parse(init?.body as string)).toEqual({ fault_class: "config_change", seed: 7 });
    expect((init?.headers as Record<string, string>)["Content-Type"]).toBe("application/json");
  });
});

describe("verify mapping", () => {
  const proofs = reduceAll(HERO_EVENTS).proofs;
  it("keeps UNANCHORED as a chain-level state; per-proof results stay verified", () => {
    const m = mapVerify(
      { overall: "UNANCHORED", chain_ok: true, anchor_ok: false, tampered_reasons: [], proofs: proofs.map((p) => ({ seq: p.position, kind: "hypothesis", entity_id: p.hypothesis_id, status: "VERIFIED", responsible: [], late_arriving: false })) },
      proofs,
    );
    expect(m.chain.result.state).toBe("UNANCHORED");
    expect(Object.values(m.byHypothesis).every((r) => r.state === "VERIFIED")).toBe(true);
  });
  it("surfaces tampered reasons", () => {
    const m = mapVerify(
      { overall: "TAMPERED", chain_ok: false, anchor_ok: true, tampered_reasons: ["hash mismatch at seq 2"], proofs: [{ seq: 2, kind: "hypothesis", entity_id: proofs[1].hypothesis_id, status: "TAMPERED", responsible: ["x"], late_arriving: false, tampered_reason: "hash mismatch at seq 2" }] },
      proofs,
    );
    expect(m.chain.result.reasons).toEqual(["hash mismatch at seq 2"]);
    expect(m.byHypothesis[proofs[1].hypothesis_id]).toMatchObject({ state: "TAMPERED", detail: "hash mismatch at seq 2" });
  });
  it("does not disturb the reducer-derived banners", () => {
    expect(deriveBanners(reduceAll(HERO_EVENTS)).map((b) => b.kind)).toEqual(["diagnosed"]);
  });
});

describe("exonerate parsing", () => {
  it("reads a causal path and a receipt", () => {
    expect(parseExonerate({ path: ["a", { id: "b" }] }, "chg-1")).toEqual({ kind: "path", change: "chg-1", nodes: ["a", "b"] });
    expect(parseExonerate({ receipt: { failing_clause: "origin", service: "svc", hash: "h" } }, "chg-2")).toMatchObject({ kind: "receipt", clause: "origin", service: "svc" });
    expect(parseExonerate("nope", "c")).toBeNull();
  });
});

describe("POST requests", () => {
  it("always send a JSON content type and body (API rejects non-JSON with 415)", async () => {
    const calls: { init: RequestInit }[] = [];
    const f = async (_i: RequestInfo | URL, init?: RequestInit) => {
      calls.push({ init: init ?? {} });
      return new Response(JSON.stringify({ created: true, idle_ttl_s: 60 }), { status: 200 });
    };
    await createSession(f);
    const headers = calls[0].init.headers as Record<string, string>;
    expect(headers["Content-Type"]).toBe("application/json");
    expect(calls[0].init.body).toBe("{}");
  });
});

describe("getRecordedHero", () => {
  it("unwraps the API's {recorded, events} envelope", async () => {
    const f = async () =>
      new Response(JSON.stringify({ recorded: true, events: [{ id: 1, type: "done" }] }), { status: 200 });
    const evs = await getRecordedHero(f);
    expect(evs).toHaveLength(1);
  });
});

describe("error body shapes", () => {
  const msg = async (status: number, body: unknown) => {
    const e = await exonerate("i1", "c1", json(status, body)).catch((x) => x);
    return e as NetworkError;
  };
  it("reads {error}", async () => {
    const e = await msg(422, { error: "unknown change" });
    expect(e).toBeInstanceOf(NetworkError);
    expect(e.message).toBe("unknown change");
    expect(e.status).toBe(422);
  });
  it("reads {detail} string", async () => {
    const e = await msg(404, { detail: "no such incident" });
    expect(e.message).toBe("no such incident");
    expect(e.status).toBe(404);
  });
  it("summarises FastAPI validation arrays", async () => {
    const e = await msg(422, { detail: [{ loc: ["body", "change_id"], msg: "Field required" }, { loc: ["body", "x"], msg: "bad" }] });
    expect(e.message).toBe("body.change_id: Field required; body.x: bad");
  });
  it("prefers reason over error over detail", async () => {
    const e = await msg(400, { reason: "r", error: "e", detail: "d" });
    expect(e.message).toBe("r");
  });
  it("falls back to the generic message", async () => {
    expect((await msg(500, {})).message).toMatch(/failed with 500/);
  });
});

describe("retryable 409", () => {
  const body = { error: "still building", retry: true };
  it("throws RetryableError", async () => {
    const e = await exonerate("i1", "c1", json(409, body), { attempts: 1 }).catch((x) => x);
    expect(e).toBeInstanceOf(RetryableError);
    expect(e.status).toBe(409);
    expect(e.message).toBe("still building");
  });
  it("retries with backoff then succeeds", async () => {
    let n = 0;
    const waits: number[] = [];
    const f = async () => (++n < 3 ? new Response(JSON.stringify(body), { status: 409 }) : new Response(JSON.stringify({ result: 1 }), { status: 200 }));
    const r = await getIncidentReplay("i1", "known", f, { sleep: async (ms) => void waits.push(ms) });
    expect(r).toEqual({ result: 1 });
    expect(n).toBe(3);
    expect(waits).toEqual([400, 800]);
  });
  it("gives up after 5 tries", async () => {
    let n = 0;
    const f = async () => (n++, new Response(JSON.stringify(body), { status: 409 }));
    const e = await exonerate("i1", "c1", f, { sleep: async () => {} }).catch((x) => x);
    expect(e).toBeInstanceOf(RetryableError);
    expect(n).toBe(5);
  });
  it("does not retry other 409s", async () => {
    let n = 0;
    const f = async () => (n++, new Response(JSON.stringify({ error: "conflict" }), { status: 409 }));
    const e = await exonerate("i1", "c1", f, { sleep: async () => {} }).catch((x) => x);
    expect(e).toBeInstanceOf(NetworkError);
    expect(n).toBe(1);
  });
});

describe("mapVerify matching", () => {
  const proofs = reduceAll(HERO_EVENTS).proofs;
  const base = { overall: "VERIFIED" as const, chain_ok: true, anchor_ok: true, tampered_reasons: [] };
  it("matches by seq, never by index", () => {
    const m = mapVerify({ ...base, proofs: [{ seq: 999, kind: "k", entity_id: "zzz", status: "FALSIFIED", responsible: [], late_arriving: false }] }, proofs);
    expect(m.byHypothesis).toEqual({});
  });
  it("falls back to entity_id + kind", () => {
    const p = proofs[0];
    const m = mapVerify({ ...base, proofs: [{ seq: 999, kind: "hypothesis", entity_id: p.hypothesis_id, status: "DRIFTED", responsible: [], late_arriving: false }] }, proofs);
    expect(m.byHypothesis[p.hypothesis_id].state).toBe("DRIFTED");
  });
  it("matches by seq when entity_id differs", () => {
    const p = proofs[0];
    const m = mapVerify({ ...base, proofs: [{ seq: p.position, kind: "k", entity_id: "other", status: "DRIFTED", responsible: [], late_arriving: false }] }, proofs);
    expect(m.byHypothesis[p.hypothesis_id].state).toBe("DRIFTED");
  });
});
