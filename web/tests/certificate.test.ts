import { describe, expect, it } from "vitest";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import type { CertificateData } from "../lib/payloads";
import { Certificate, clauseSegments } from "../components/Certificate";

const BASE: CertificateData = {
  services_checked: 4,
  window: { from: "2026-03-01T10:00:00Z", to: "2026-03-01T11:10:00Z" },
  qualifying: 0,
  suspect: "chg-root",
  checked: [
    { change: "chg-cov", service: "svc-b@us-east", clause: "coverage" },
    { change: "chg-late", service: "svc-c@us-east", clause: "after_onset" },
  ],
  hash: "ab".repeat(32),
};

const TIMED: CertificateData = {
  ...BASE,
  onset: "2026-03-01T11:00:00Z",
  suspect_event_time: "2026-03-01T10:50:00Z",
  checked: [
    { ...BASE.checked[0], event_time: "2026-03-01T10:40:00Z" },
    { ...BASE.checked[1], event_time: "2026-03-01T11:05:00Z" },
  ],
};

const render = (cert: CertificateData) =>
  renderToStaticMarkup(createElement(Certificate, { cert, view: "true", onFocus: () => {} }));

const states = (html: string) => [...html.matchAll(/data-state="(\w+)"/g)].map((m) => m[1]);

describe("clause bars", () => {
  it("maps a coverage failure to two passed, one failed, two not reached (AE5)", () => {
    expect(clauseSegments("coverage")).toEqual(["passed", "passed", "failed", "unreached", "unreached"]);
  });

  it("fills all five for the root", () => {
    expect(clauseSegments(null)).toEqual(["passed", "passed", "passed", "passed", "passed"]);
  });

  it("renders the root bar first and one bar per candidate", () => {
    const s = states(render(BASE));
    expect(s.length).toBe(15);
    expect(s.slice(0, 5)).toEqual(["passed", "passed", "passed", "passed", "passed"]);
    // sorted deepest clause first: coverage then after_onset
    expect(s.slice(5, 10)).toEqual(["passed", "passed", "failed", "unreached", "unreached"]);
    expect(s.slice(10)).toEqual(["failed", "unreached", "unreached", "unreached", "unreached"]);
  });
});

describe("time strip", () => {
  it("is hidden for payloads without onset", () => {
    const html = render(BASE);
    expect(html).not.toContain("time-strip");
    expect(html).toContain("clause-bar");
  });

  it("marks onset and flags changes after it", () => {
    const html = render(TIMED);
    expect(html).toContain("time-strip");
    expect(html).toContain('data-mark="onset"');
    expect(html).toMatch(/title="chg-late [^"]*\(after onset\)"/);
    expect(html).not.toMatch(/title="chg-cov [^"]*\(after onset\)"/);
    expect(html).toContain("1 change after onset");
  });
});

describe("suspect row", () => {
  it("says passes all only when the suspect has no failing clause", () => {
    expect(render(BASE)).toContain("passes all");
    const html = render({ ...BASE, suspect_clause: "order" });
    expect(html).not.toContain("passes all");
    expect(states(html).slice(0, 5)).toEqual(["passed", "passed", "passed", "failed", "unreached"]);
    expect(render({ ...BASE, suspect_clause: null })).toContain("passes all");
  });
});
