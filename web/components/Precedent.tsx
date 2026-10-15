"use client";

import type { PrecedentMatch } from "@/lib/payloads";
import { Badge, Empty } from "./ui";

const METHOD: Record<PrecedentMatch["method"], string> = {
  semantic: "semantic similarity",
  fuzzy: "fuzzy text match",
  path_shape: "same causal-path shape",
};

const FIELD = { vector: "semantic", fulltext: "fuzzy", fingerprint: "path shape" } as const;

export function Precedent({ entries }: { entries: { text: string; matches: PrecedentMatch[] }[] }) {
  if (!entries.length) return <Empty>Past incidents are matched by meaning, by wording, and by causal-path shape. Matches are advisory and stay outside the proof chain.</Empty>;
  return (
    <div className="space-y-3 overflow-y-auto px-inset py-3">
      {entries.map((e, i) => (
        <div key={i}>
          <p className="max-w-[80ch] text-body text-ink/90">{e.text}</p>
          <ul className="mt-2 divide-y divide-rule/70 border border-rule">
            {e.matches.map((m) => (
              <li key={m.incident} className="grid grid-cols-[84px_minmax(0,1fr)] items-baseline gap-x-3 gap-y-1 px-3 py-1.5 sm:grid-cols-[84px_minmax(0,1fr)_auto]">
                <span className="font-mono text-label text-ink">{m.incident}</span>
                <span className="text-label">
                  {m.title}
                  {m.cause && <span className="block text-meta text-dim">{m.cause}</span>}
                  {m.resolved_minutes != null && <span className="block text-meta text-faint">resolved in {m.resolved_minutes} min</span>}
                </span>
                <span className="col-start-2 flex flex-wrap items-center gap-2 sm:col-start-auto">
                  {m.methods && Object.keys(m.methods).length > 1 ? (
                    (Object.entries(m.methods) as [keyof typeof FIELD, number][]).map(([k, v]) => (
                      <Badge key={k} tone="info">{FIELD[k]} {v.toFixed(2)}</Badge>
                    ))
                  ) : (
                    <Badge tone="info">{METHOD[m.method]}</Badge>
                  )}
                  <span className="font-mono text-meta tabular-nums text-dim">{m.score.toFixed(2)}</span>
                </span>
              </li>
            ))}
          </ul>
        </div>
      ))}
      <p className="text-meta text-faint">Advisory. Precedent results are not hashed into the chain.</p>
    </div>
  );
}
