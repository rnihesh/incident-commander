import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { Challenge, ChallengeResultView } from "../components/Challenge";
import { CLAUSE_LABELS, acceptFetchedBudget, challengeOptions, clauseLabel, parseChallenge, verdictLabel, type ChallengeResponse } from "../lib/challenge";
import { NetworkError, RateLimitedError, challengeIncident, getChallengeBudget } from "../lib/api";
import { CLAUSES, type CertificateData, type VerdictData } from "../lib/payloads";

const body = (o: Partial<ChallengeResponse> = {}): ChallengeResponse => ({
  incident_id: "inc-041",
  change_id: "chg-05700",
  is_root: false,
  diagnosed_root: "chg-05689",
  verdict: "REFUTED",
  clause: "after_onset",
  detail: "event at 1790824900 is not before onset 1790824858",
  query_text: "MATCH (c:Change {id: $entity_id})-[:CHANGES]->(home:Service) RETURN c",
  rivals: [],
  claim: { service_id: "svc-object-store-catalog@eu-west", event_time: 1790824900 },
  would_overturn: null,
  limit: 2,
  remaining: 1,
  ...o,
});

const cert: CertificateData = {
  services_checked: 12,
  window: { from: "2026-10-01T01:37:38Z", to: "2026-10-01T03:40:00Z" },
  qualifying: 0,
  suspect: "chg-05689",
  checked: [
    { change: "chg-05687", service: "svc-a@eu-west", clause: "origin" },
    { change: "chg-05700", service: "svc-b@eu-west", clause: "after_onset" },
  ],
  hash: "h",
};
const verdict = {
  outcome: "DIAGNOSED",
  headline: "x",
  root_cause: { change: "chg-05689", service: "svc-r@eu-west", kind: "deploy", summary: "s", known_at_alert: true },
} as unknown as VerdictData;

const ok = (payload: unknown, status = 200) => {
  const calls: { url: string; init?: RequestInit }[] = [];
  const f = async (url: string, init?: RequestInit) => {
    calls.push({ url, init });
    return new Response(JSON.stringify(payload), { status });
  };
  return { calls, f };
};

describe("parseChallenge", () => {
  it("reads a refutation with its clause, detail and query", () => {
    const r = parseChallenge(body());
    expect(r).toMatchObject({ kind: "verdict", verdict: "REFUTED", clause: "after_onset", remaining: 1, wouldOverturn: false });
    if (r?.kind !== "verdict") throw new Error("expected a verdict");
    expect(r.queryText).toContain("MATCH");
    expect(r.service).toBe("svc-object-store-catalog@eu-west");
    expect(verdictLabel(r)).toBe("REFUTED · after onset");
  });
  it("flags a qualifying challenge as would-overturn", () => {
    const r = parseChallenge(body({ verdict: "RIVAL", clause: null, would_overturn: "INCONCLUSIVE", rivals: [{ id: "chg-05689" }], remaining: 0 }));
    expect(r).toMatchObject({ kind: "verdict", verdict: "RIVAL", clause: null, wouldOverturn: true, rivals: ["chg-05689"], remaining: 0 });
    if (r?.kind !== "verdict") throw new Error("expected a verdict");
    expect(verdictLabel(r)).toMatch(/^QUALIFIES/);
    expect(verdictLabel({ ...r, verdict: "VERIFIED" })).toMatch(/^QUALIFIES/);
  });
  it("reads the diagnosed-root answer", () => {
    const r = parseChallenge(body({ is_root: true, change_id: "chg-05689", verdict: null, message: "chg-05689 is the diagnosed root", remaining: 2 }));
    expect(r).toEqual({ kind: "root", change: "chg-05689", message: "chg-05689 is the diagnosed root", remaining: 2, limit: 2 });
  });
  it("rejects unrecognisable bodies", () => {
    expect(parseChallenge(null)).toBeNull();
    expect(parseChallenge("nope")).toBeNull();
    expect(parseChallenge({ change_id: "c", verdict: "MAYBE" })).toBeNull();
  });
});

describe("clause labels", () => {
  it("cover every predicate clause plus the window and service cases", () => {
    for (const c of [...CLAUSES, "outside_window", "not_a_change"]) expect(clauseLabel(c)).toBeTruthy();
    expect(Object.keys(CLAUSE_LABELS)).toHaveLength(CLAUSES.length + 2);
    expect(clauseLabel("after_onset")).toMatch(/after the first symptom/);
    expect(clauseLabel("nope")).toBeNull();
    expect(clauseLabel(null)).toBeNull();
  });
});

describe("challengeOptions", () => {
  it("lists the root first, then eliminated changes newest first", () => {
    expect(challengeOptions(cert, verdict).map((o) => [o.change, o.root])).toEqual([
      ["chg-05689", true],
      ["chg-05700", false],
      ["chg-05687", false],
    ]);
    expect(challengeOptions(null, verdict)).toEqual([]);
  });
});

describe("challenge client", () => {
  it("posts the change id as JSON to the incident's challenge route", async () => {
    const { calls, f } = ok(body());
    const r = await challengeIncident("inc-041", "chg-05700", f);
    expect(r.verdict).toBe("REFUTED");
    expect(calls[0].url).toMatch(/\/api\/incidents\/inc-041\/challenge$/);
    expect(calls[0].init?.method).toBe("POST");
    expect(JSON.parse(String(calls[0].init?.body))).toEqual({ change_id: "chg-05700" });
  });
  it("maps the third challenge (429) to RateLimitedError with the reason", async () => {
    const { f } = ok({ error: "two challenges per incident", reason: "both challenges are used", remaining: 0 }, 429);
    const e = await challengeIncident("inc-041", "chg-05700", f).catch((x) => x);
    expect(e).toBeInstanceOf(RateLimitedError);
    expect(e.reason).toBe("both challenges are used");
  });
  it("surfaces a non-candidate (422) as an error message", async () => {
    const { f } = ok({ error: "chg-1 is not one of the changes this incident's run checked" }, 422);
    const e = await challengeIncident("inc-041", "chg-1", f).catch((x) => x);
    expect(e).toBeInstanceOf(NetworkError);
    expect(e.message).toMatch(/not one of the changes/);
  });
  it("reads the remaining budget", async () => {
    const { calls, f } = ok({ incident_id: "inc-041", limit: 2, remaining: 2 });
    expect(await getChallengeBudget("inc-041", f)).toEqual({ incident_id: "inc-041", limit: 2, remaining: 2 });
    expect(calls[0].init?.method ?? "GET").toBe("GET");
  });
});

describe("Challenge panel", () => {
  const view = (b: ChallengeResponse) => renderToStaticMarkup(createElement(ChallengeResultView, { result: parseChallenge(b)! }));

  it("shows a refutation with its plain-English clause, query and remaining count", () => {
    const html = view(body());
    expect(html).toContain("REFUTED · after onset");
    expect(html).toContain("after the first symptom");
    expect(html).toContain("Change");
    expect(html).toContain("1 of 2 challenges left");
    expect(html).not.toContain("would overturn");
  });
  it("shows the would-overturn banner for a qualifying change", () => {
    const html = view(body({ verdict: "RIVAL", clause: null, would_overturn: "INCONCLUSIVE", rivals: [{ id: "chg-05689" }], remaining: 0 }));
    expect(html).toContain("would overturn: INCONCLUSIVE");
    expect(html).toContain("chg-05689");
    expect(html).toContain("0 of 2 challenges left");
    expect(html).toMatch(/verdict stands until/i);
  });
  it("answers the diagnosed root without spending", () => {
    const html = view(body({ is_root: true, change_id: "chg-05689", verdict: null, message: "chg-05689 is the diagnosed root", remaining: 2 }));
    expect(html).toContain("this is the diagnosed root");
    expect(html).toContain("2 of 2 challenges left");
  });
  it("says challenges need a live incident instead of answering from mocks", () => {
    const html = renderToStaticMarkup(createElement(Challenge, { cert, verdict, incidentId: null }));
    expect(html).toMatch(/live incident/);
    expect(html).not.toContain("<select");
  });
  it("offers the pick list for a live incident", () => {
    const html = renderToStaticMarkup(createElement(Challenge, { cert, verdict, incidentId: "inc-041" }));
    expect(html).toContain("<select");
    expect(html).toContain("chg-05700");
  });
  it("has no mock receipt path", () => {
    const src = readFileSync(join(__dirname, "../components/Challenge.tsx"), "utf8");
    expect(src).not.toMatch(/fakeHash|lib\/mock/);
  });
});

describe("acceptFetchedBudget", () => {
  it("drops a late budget GET once ask() has written a fresher budget for that incident", () => {
    expect(acceptFetchedBudget(null, "inc-1")).toBe(true);
    expect(acceptFetchedBudget("inc-1", "inc-1")).toBe(false);
    expect(acceptFetchedBudget("inc-1", "inc-2")).toBe(true);
  });
});
