"use client";

import type { ReflexView } from "@/lib/reducer";
import { Badge } from "./ui";
import { shortSvc } from "./ui";

export function Reflex({ reflex }: { reflex: ReflexView | null }) {
  if (!reflex) return null;
  const sup = reflex.superseded;
  return (
    <section
      aria-label="Reflex answer, pre-snapshot"
      className={`arrive flex shrink-0 flex-wrap items-center gap-x-5 gap-y-1.5 border border-dashed px-inset py-2 text-label ${
        sup ? "border-rule bg-transparent text-faint" : "border-symptom/50 bg-symptom/5 text-dim"
      }`}
    >
      <span className="flex items-center gap-2">
        <Badge tone={sup ? "muted" : "info"}>{sup ? "SUPERSEDED" : "PRE-SNAPSHOT"}</Badge>
        <span className="font-mono">reflex {reflex.ms} ms</span>
      </span>
      {(reflex.blast_radius != null || reflex.search_space != null) && (
        <span data-testid="reflex-scope">
          {reflex.blast_radius != null && (
            <>
              Symptomatic <b className={`font-mono font-medium ${sup ? "" : "text-ink"}`}>{reflex.blast_radius}</b>
            </>
          )}
          {reflex.blast_radius != null && reflex.search_space != null && <span className="px-1.5 text-faint">&middot;</span>}
          {reflex.search_space != null && (
            <>
              Search space <b className={`font-mono font-medium ${sup ? "" : "text-ink"}`}>{reflex.search_space}</b> services
            </>
          )}
        </span>
      )}
      <span className="flex flex-wrap items-center gap-x-3">
        Top suspects
        {reflex.suspects.map((s) => (
          <span key={s.change} className="font-mono" title={`${s.summary} on ${s.service}`}>
            <span className={sup ? "" : "text-ink"}>{s.change}</span> <span className="text-faint">{shortSvc(s.service)}</span>
          </span>
        ))}
      </span>
      <span>
        On call <b className={`font-medium ${sup ? "" : "text-ink"}`}>{reflex.oncall.name}</b>
      </span>
      {sup && (
        <span className="ml-auto flex items-center gap-2">
          {reflex.diff ? (
            <Badge tone="drifted" title="Suspects changed between the reflex answer and the first funnel cut">
              SUSPECTS CHANGED
              {reflex.diff.added.length > 0 && ` +${reflex.diff.added.join(" +")}`}
              {reflex.diff.removed.length > 0 && ` -${reflex.diff.removed.join(" -")}`}
            </Badge>
          ) : (
            <Badge tone="verified">SUSPECTS CONFIRMED</Badge>
          )}
        </span>
      )}
    </section>
  );
}
