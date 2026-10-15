"use client";

import { useState } from "react";
import type { FunnelData } from "@/lib/payloads";
import { xrayStep } from "@/lib/api";
import { Cypher, Empty, PanelHead, CopyButton } from "./ui";

/** `incidentId`: the live incident the cuts belong to (enables X-ray); null for recordings. */
export function Funnel({
  steps,
  onFocus,
  incidentId = null,
}: {
  steps: FunnelData[];
  onFocus: (ids: string[]) => void;
  incidentId?: string | null;
}) {
  const [open, setOpen] = useState<number | null>(null);
  const start = steps[0]?.before ?? 0;
  const last = steps[steps.length - 1];
  return (
    <section aria-labelledby="funnel-h" className="flex min-h-0 flex-col">
      <PanelHead
        id="funnel-h"
        title="Elimination funnel"
        meta={steps.length ? `${start} changes in window, ${last.after} remaining` : undefined}
      />
      {!steps.length ? (
        <Empty>The funnel starts once the snapshot exists. Each cut shows the query that made it.</Empty>
      ) : (
        <ol className="min-h-0 flex-1 overflow-y-auto">
          {steps.map((s, i) => {
            const w = Math.max(2.5, (s.after / Math.max(start, 1)) * 100);
            const isOpen = open === i;
            return (
              <li key={s.step} className="arrive border-b border-rule/70">
                <button
                  type="button"
                  aria-expanded={isOpen}
                  onClick={() => {
                    setOpen(isOpen ? null : i);
                    onFocus([...new Set(s.eliminated.map((e) => e.service))]);
                  }}
                  className="grid w-full grid-cols-[minmax(176px,220px)_minmax(0,1fr)_76px] items-center gap-4 px-inset py-2 text-left hover:bg-raise"
                >
                  <span className="min-w-0">
                    <span className="block font-mono text-label text-ink">{s.clause}</span>
                    <span className="block text-meta text-dim">{s.label}</span>
                  </span>
                  <span className="relative h-3.5 overflow-hidden rounded-[2px] bg-rule/50" aria-hidden>
                    <span className="absolute inset-y-0 left-0 bg-symptom/30" style={{ width: `${(s.before / Math.max(start, 1)) * 100}%` }} />
                    <span className="absolute inset-y-0 left-0 border-r-2 border-symptom bg-symptom/60" style={{ width: `${w}%` }} />
                  </span>
                  <span className="text-right font-mono text-label tabular-nums">
                    <span className="text-faint">{s.before}</span>
                    <span className="text-faint"> to </span>
                    <span className="text-ink">{s.after}</span>
                    <span className="block text-meta text-dim">{s.before - s.after === 0 ? "none cut" : `-${s.before - s.after}`}</span>
                  </span>
                </button>
                {isOpen && (
                  <div className="space-y-1.5 px-inset pb-3">
                    <div className="flex items-center justify-between gap-3 text-meta text-dim">
                      <span>{s.eliminated.length} eliminated by this query, top suspects now {s.top_suspects.join(", ")}</span>
                      <CopyButton text={s.cypher} label="Copy query" />
                    </div>
                    <Cypher
                      text={s.cypher}
                      params={s.params}
                      xray={incidentId && s.cypher ? () => xrayStep(incidentId, { funnel: s.clause }) : undefined}
                    />
                  </div>
                )}
              </li>
            );
          })}
        </ol>
      )}
    </section>
  );
}
