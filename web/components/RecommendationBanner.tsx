"use client";

import type { RecommendationRef } from "@/lib/payloads";
import { Badge, shortSvc } from "./ui";

const upper = (id?: string | null) => (id ?? "").toUpperCase();

/** The sentence the banner shows; the backend's `banner` field says the same. */
export function recommendationLine(r: RecommendationRef): string {
  const src = r.incident_id ? ` (from ${upper(r.incident_id)} postmortem)` : "";
  const days = `${r.days_open} ${r.days_open === 1 ? "day" : "days"}`;
  return `Predicted by ${r.display_id || upper(r.id)}${src}, open ${days}: ${r.text}`;
}

/** R23: open postmortem recommendations that target the causal path, beside the verdict. */
export function RecommendationBanner({ recs }: { recs: RecommendationRef[] }) {
  if (!recs.length) return null;
  return (
    <section aria-label="Open recommendations on the causal path" className="arrive border-b border-rule bg-drifted/5 px-inset py-2.5">
      <ul className="space-y-1.5">
        {recs.map((r) => (
          <li key={r.id} className="grid grid-cols-[auto_1fr] items-baseline gap-3">
            <Badge tone="drifted" title="An open recommendation from an earlier postmortem targets a service on this path">
              PREDICTED
            </Badge>
            <span className="min-w-0 text-label text-ink/90">
              {recommendationLine(r)}
              {r.services.length > 0 && (
                <span className="block font-mono text-meta text-faint">targets {r.services.map(shortSvc).join(", ")}</span>
              )}
            </span>
          </li>
        ))}
      </ul>
      <p className="mt-1.5 text-meta text-faint">Advisory. Cited in the rollback PR and the page; not part of the proof chain.</p>
    </section>
  );
}
