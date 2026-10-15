import { createHash } from "node:crypto";
import { describe, expect, it } from "vitest";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { CommitReveal } from "../components/CommitReveal";
import { TamperPanel } from "../components/TamperPanel";
import {
  EMPTY_TAMPER_FORM,
  JudgeError,
  buildTamperPayload,
  describeTamper,
  flipText,
  flippedProofs,
  fragilityMap,
  fragilityStale,
  hashCheck,
  tamperFormFor,
  trySuggestions,
  type FragilityEdit,
  type FragilityResponse,
  judgeInject,
  recomputeCommitment,
  rederive,
  revealFault,
  tamper,
  type Comparison,
  type RevealResponse,
} from "../lib/judge";
import { verifyIncident } from "../lib/api";

const PRE = '{"fault_class":"bad_deploy","fault_id":"flt-1","incident_id":"inc-004","root_id":"chg-00042"}';
const NONCE = "0123456789abcdef0123456789abcdef";
const HASH = createHash("sha256").update(PRE + NONCE).digest("hex");

const cmp = (o: Partial<Comparison> = {}): Comparison => ({
  run_id: "run-1", finished: true, outcome: "DIAGNOSED", suspect_id: "chg-00042", rival_ids: [],
  claimed_class: "bad_deploy", headline: null, result: "match", explanation: "the agent named the committed root chg-00042", ...o,
});
const reveal = (o: Partial<RevealResponse> = {}): RevealResponse => ({
  incident_id: "inc-004",
  published_commitment: HASH,
  revealed: { fault_id: "flt-1", fault_class: "bad_deploy", root_id: "chg-00042", nonce: NONCE },
  preimage: PRE,
  commitment: HASH,
  recomputed: HASH,
  commitment_ok: true,
  timed_out: false,
  elapsed_s: 40,
  comparison: cmp(),
  rederived: null,
  ...o,
});
const render = (props: Partial<Parameters<typeof CommitReveal>[0]>) =>
  renderToStaticMarkup(
    createElement(CommitReveal, {
      commitment: HASH, incidentId: "inc-004", canReveal: false, secondsLeft: 87, reveal: null,
      browserHash: null, onReveal: () => {}, revealing: false, error: null, ...props,
    }),
  );
const capture = () => {
  const calls: { url: string; init?: RequestInit }[] = [];
  const f = async (url: string, init?: RequestInit) => {
    calls.push({ url, init });
    return new Response(JSON.stringify({ ok: true, proofs: [], status: "FALSIFIED" }), { status: 200 });
  };
  return { calls, f };
};

describe("commitment", () => {
  it("recomputes sha256(preimage + nonce) in the browser", async () => {
    expect(await recomputeCommitment(PRE, NONCE)).toBe(HASH);
    expect(await recomputeCommitment(PRE, "x")).not.toBe(HASH);
  });
  it("checks published vs recomputed, server and browser", () => {
    expect(hashCheck(HASH, reveal(), HASH)).toEqual({ server: true, browser: true, ok: true });
    expect(hashCheck(HASH, reveal(), null).ok).toBe(true);
    expect(hashCheck(HASH, reveal(), "bad").ok).toBe(false);
    expect(hashCheck("other", reveal(), null).ok).toBe(false);
  });
});

describe("CommitReveal rendering", () => {
  it("shows the commitment and a countdown before the reveal, never the answer", () => {
    const html = render({});
    expect(html).toContain(HASH);
    expect(html).toContain("Reveal in 87s");
    expect(html).toContain("disabled");
    expect(html).not.toContain("chg-00042");
    expect(html).not.toContain("bad_deploy");
  });
  it("enables Reveal at the verdict", () => {
    const html = render({ canReveal: true });
    expect(html).toMatch(/>Reveal</);
  });
  it("renders MATCH side by side with the recomputed hash", () => {
    const html = render({ reveal: reveal(), browserHash: HASH });
    expect(html).toContain("Committed fault");
    expect(html).toContain("Agent verdict (blind)");
    expect(html).toContain("MATCH");
    expect(html).toContain("chg-00042");
    expect(html).toContain("Recomputed hash = committed hash");
    expect(html).toContain("also recomputed in this browser");
  });
  it("renders PARTIAL and NO CLAIM, and flags a hash mismatch", () => {
    const partial = render({ reveal: reveal({ comparison: cmp({ result: "partial", suspect_id: "chg-00050" }) }) });
    expect(partial).toContain("PARTIAL");
    expect(partial).not.toContain(">MATCH<");
    const none = render({ reveal: reveal({ timed_out: true, comparison: cmp({ result: "no_claim", outcome: null, suspect_id: null, claimed_class: null }) }) });
    expect(none).toContain("NO CLAIM");
    expect(none).toContain("120 s timeout");
    const bad = render({ reveal: reveal({ recomputed: "f".repeat(64) }) });
    expect(bad).toContain("differs from the committed hash");
  });
  it("shows the re-derived verdict beside the blind one", () => {
    const html = render({ reveal: reveal({ rederived: cmp({ result: "no_claim", outcome: "INCONCLUSIVE", run_id: "run-2" }) }) });
    expect(html).toContain("Re-derived verdict");
    expect(html).toContain("INCONCLUSIVE");
  });
});

describe("tamper payloads", () => {
  it("builds add_deploy_upstream with integer minutes and the known-at-alert flag", () => {
    const p = buildTamperPayload("add_deploy_upstream", { ...EMPTY_TAMPER_FORM, service_id: "svc-a@us-east", minutes_before_onset: "7", as_if_known_at_alert: false }, 1000);
    expect(p).toEqual({ action: "add_deploy_upstream", service_id: "svc-a@us-east", minutes_before_onset: 7, as_if_known_at_alert: false });
    expect(describeTamper(p as never)).toContain("late-arriving");
    expect(buildTamperPayload("add_deploy_upstream", { ...EMPTY_TAMPER_FORM, service_id: "s@r", minutes_before_onset: "1.5" }, 1000)).toHaveProperty("error");
    expect(buildTamperPayload("add_deploy_upstream", EMPTY_TAMPER_FORM, 1000)).toHaveProperty("error");
  });
  it("builds remove_edge and change_timestamp (relative to the earliest onset)", () => {
    expect(buildTamperPayload("remove_edge", { ...EMPTY_TAMPER_FORM, from_service_id: "a@r", to_service_id: "b@r" }, null)).toEqual({
      action: "remove_edge", from_service_id: "a@r", to_service_id: "b@r",
    });
    expect(buildTamperPayload("remove_edge", { ...EMPTY_TAMPER_FORM, from_service_id: "a@r", to_service_id: "a@r" }, null)).toHaveProperty("error");
    expect(buildTamperPayload("change_timestamp", { ...EMPTY_TAMPER_FORM, change_id: "chg-00042", shift_minutes: "-3" }, 10_000)).toEqual({
      action: "change_timestamp", change_id: "chg-00042", new_valid_from: 10_000 - 180,
    });
  });
  it("posts exactly the action as JSON to the session-scoped incident route", async () => {
    const { calls, f } = capture();
    const action = { action: "add_deploy_upstream" as const, service_id: "svc-a@us-east", minutes_before_onset: 3, as_if_known_at_alert: true };
    await tamper("inc-004", action, f);
    expect(calls[0].url).toMatch(/\/api\/judge\/inc-004\/tamper$/);
    expect(calls[0].init?.method).toBe("POST");
    expect(calls[0].init?.credentials).toBe("include");
    expect((calls[0].init?.headers as Record<string, string>)["Content-Type"]).toBe("application/json");
    expect(JSON.parse(calls[0].init?.body as string)).toEqual(action);
    await rederive("inc/../x", f);
    expect(calls[1].url).toMatch(/\/api\/judge\/inc%2F..%2Fx\/rederive$/);
    await revealFault("inc-004", f);
    expect(calls[2].url).toMatch(/\/reveal$/);
    await judgeInject("svc-a@us-east", "expiry", f);
    expect(JSON.parse(calls[3].init?.body as string)).toEqual({ target_service_id: "svc-a@us-east", fault_class: "expiry" });
  });
  it("surfaces the server's reason and retry_after", async () => {
    const f = async () => new Response(JSON.stringify({ error: "too early", retry_after: 42 }), { status: 425 });
    const e = await revealFault("inc-004", f).catch((x) => x);
    expect(e).toBeInstanceOf(JudgeError);
    expect(e).toMatchObject({ status: 425, retryAfter: 42, message: "too early" });
    const g = async () => new Response(JSON.stringify({ reason: "svc cannot produce an alert path: nothing depends on it" }), { status: 422 });
    expect((await judgeInject("s@r", "bad_deploy", g).catch((x) => x)).message).toContain("alert path");
  });
  it("renders the panel's three actions and the falsified proofs with their responsible node", () => {
    const html = renderToStaticMarkup(
      createElement(TamperPanel, {
        incidentId: "inc-004", onset: 1000, services: [{ id: "svc-a@us-east", label: "a" }], edges: [{ source: "svc-a@us-east", target: "svc-b@us-east" }],
        changes: [{ id: "chg-00042", service_id: "svc-a@us-east", kind: "deploy" }], selectedNode: null, disabled: false,
        onSubmit: async () => { throw new Error("unused"); }, log: [], onVerify: () => {}, verifying: false,
        verify: { overall: "FALSIFIED", chain_ok: true, anchor_ok: true, tampered_reasons: [], proofs: [
          { seq: 3, kind: "absence", entity_id: "chg-00042", status: "FALSIFIED", responsible: ["chg-00099"], late_arriving: false },
          { seq: 1, kind: "path", entity_id: "chg-00042", status: "VERIFIED", responsible: [], late_arriving: false },
        ] },
        onRederive: () => {}, rederiving: false, onFocusNode: () => {},
      }),
    );
    for (const label of ["Add deploy upstream", "Remove edge", "Change timestamp", "Verify", "Re-derive"]) expect(html).toContain(label);
    expect(html).toContain("chg-00099");
    expect(html).toContain("FALSIFIED");
  });
});

describe("fragility map: try these", () => {
  const edit = (o: Partial<FragilityEdit>): FragilityEdit => ({
    kind: "remove_edge", label: "Remove the hop db@r to api@r", tamper: null, prediction: { exact: true, note: "" },
    verdict: "REFUTED", clause: "coverage", detail: "", rivals: [], flips: true, copy_ms: 110, ...o,
  });
  const hop = edit({
    hop: { from: "db@r", to: "api@r" }, prediction: { exact: true, note: "both sources", sources: ["DEPENDS_ON", "OBSERVED_CALL"] },
    tamper: { action: "remove_edge", from_service_id: "api@r", to_service_id: "db@r" },
  });
  const deploy = edit({
    kind: "add_deploy_upstream", label: "Add a deploy on db@r", verdict: "RIVAL", clause: null, rivals: ["planted-deploy"],
    tamper: { action: "add_deploy_upstream", service_id: "db@r", minutes_before_onset: 5, as_if_known_at_alert: true },
  });
  const moved = edit({
    kind: "change_timestamp", label: "Move chg-00042", clause: "after_onset",
    tamper: { action: "change_timestamp", change_id: "chg-00042", new_valid_from: 10_000 + 60 },
  });
  const resp = (flips: FragilityEdit[]): FragilityResponse => ({
    incident_id: "inc-004", root_id: "chg-00042", applicable: true,
    baseline: { verdict: "VERIFIED", clause: null, detail: "", rivals: [] }, earliest_onset: 10_000,
    path: ["chg-00042", "db@r", "api@r"], tried: [...flips, edit({ flips: false, verdict: "VERIFIED", clause: null })],
    flips, copies: flips.length + 1, skipped: 0, max_edits: 10, copy_ms: 110, detail: "3 of 4 single edits would flip chg-00042",
  });

  it("pre-fills kind and every field, and the form rebuilds exactly the suggested body", () => {
    for (const e of [hop, deploy, moved]) {
      const { kind, form } = tamperFormFor(e.tamper!, 10_000);
      expect(kind).toBe(e.kind);
      expect(buildTamperPayload(kind, form, 10_000)).toEqual(e.tamper);
    }
    expect(tamperFormFor(deploy.tamper!, 10_000).form).toMatchObject({ service_id: "db@r", minutes_before_onset: "5", as_if_known_at_alert: true });
    expect(tamperFormFor(hop.tamper!, null).form).toMatchObject({ from_service_id: "api@r", to_service_id: "db@r" });
    expect(tamperFormFor(moved.tamper!, 10_000).form).toMatchObject({ change_id: "chg-00042", shift_minutes: "1" });
  });
  it("offers only flipping edits a tamper form can apply", () => {
    const orphan = edit({ tamper: null });
    expect(trySuggestions(resp([hop, orphan, deploy])).map((e) => e.label)).toEqual([hop.label, deploy.label]);
    expect(trySuggestions(null)).toEqual([]);
    expect(flipText(deploy)).toContain("planted-deploy");
    expect(flipText(hop)).toBe("REFUTED on coverage");
  });
  it("calls a map stale once a tamper or a new run landed after it started", () => {
    expect(fragilityStale(0, 0)).toBe(false);
    expect(fragilityStale(0, 1)).toBe(true);
    expect(fragilityStale(2, 2)).toBe(false);
  });
  it("posts to the session-scoped fragility route", async () => {
    const { calls, f } = capture();
    await fragilityMap("inc-004", f);
    expect(calls[0].url).toMatch(/\/api\/judge\/inc-004\/fragility$/);
    expect(calls[0].init?.method).toBe("POST");
    expect(calls[0].init?.credentials).toBe("include");
  });
  it("renders the try-these list with each predicted flip", () => {
    const html = renderToStaticMarkup(
      createElement(TamperPanel, {
        incidentId: "inc-004", onset: 10_000, services: [], edges: [], changes: [], selectedNode: null, disabled: false,
        onSubmit: async () => { throw new Error("unused"); }, log: [], onVerify: () => {}, verifying: false, verify: null,
        onRederive: () => {}, rederiving: false, onFocusNode: () => {}, initialFragility: resp([hop, deploy, moved]),
      }),
    );
    expect(html).toContain("Try these");
    for (const label of [hop.label, deploy.label, moved.label]) expect(html).toContain(label);
    expect(html).toContain("REFUTED on coverage");
    expect(html).toContain("DEPENDS_ON + OBSERVED_CALL");
    expect(html).toContain("3 of 4");
  });
});

describe("verify mapping for judge mode", () => {
  it("normalizes the backend's status field and lists flipped proofs", async () => {
    const f = async () =>
      new Response(JSON.stringify({ status: "FALSIFIED", chain_ok: true, anchor_ok: true, tampered_reasons: [], proofs: [
        { seq: 2, kind: "absence", entity_id: "chg-1", status: "FALSIFIED", responsible: ["chg-9", "svc-x@r"], late_arriving: true },
      ] }), { status: 200 });
    const r = await verifyIncident("inc-004", f);
    expect(r.overall).toBe("FALSIFIED");
    expect(flippedProofs(r)).toEqual([{ seq: 2, kind: "absence", entity_id: "chg-1", responsible: ["chg-9", "svc-x@r"], late: true }]);
    expect(flippedProofs(null)).toEqual([]);
  });
});
