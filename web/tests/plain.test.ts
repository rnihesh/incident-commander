import { describe, expect, it } from "vitest";
import { plainText } from "../lib/plain";

describe("plainText", () => {
  it("rewords the live and recorded proof claims", () => {
    expect(plainText("chg-05690 passes every KTD23 clause for inc-041")).toBe(
      "chg-05690 passes every clause of the explanation rule for inc-041",
    );
    expect(plainText("chg-05689 qualifies as the root-cause change under every KTD23 clause.")).toBe(
      "chg-05689 qualifies as the root-cause change under every clause of the explanation rule.",
    );
    expect(plainText("chg-05689 qualifies: every KTD23 clause holds against every symptom.")).toBe(
      "chg-05689 qualifies: every clause of the explanation rule holds against every symptom.",
    );
    expect(plainText("the entity fails at least one KTD23 clause (the first is recorded)")).toBe(
      "the entity fails at least one clause of the explanation rule (the first is recorded)",
    );
  });

  it("handles the predicate, the funnel and bare codes", () => {
    expect(plainText("the same causal rule (KTD23) the graph screens with")).toBe("the same causal rule the graph screens with");
    expect(plainText("graph fallback (KTD23 funnel + proofs)")).toBe("graph fallback (elimination funnel + proofs)");
    expect(plainText("Run the KTD23 predicate")).toBe("Run the explanation rule");
    expect(plainText("see KTD4")).toBe("see the explanation rule");
  });

  it("leaves other text and empty values alone", () => {
    expect(plainText("chg-05688 fails origin")).toBe("chg-05688 fails origin");
    expect(plainText(null)).toBeNull();
    expect(plainText("")).toBe("");
  });
});
