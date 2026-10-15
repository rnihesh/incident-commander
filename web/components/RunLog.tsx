"use client";

import { useState } from "react";
import type { ToolCallEntry } from "@/lib/reducer";
import { CopyButton, Cypher, Empty } from "./ui";

export function RunLog({ calls, t0 }: { calls: ToolCallEntry[]; t0: number | null }) {
  const [open, setOpen] = useState<number | null>(null);
  if (!calls.length) return <Empty>Every tool call lands here with its Cypher, parameters, nodes touched and elapsed time.</Empty>;
  const total = calls.reduce((a, c) => a + c.ms, 0);
  return (
    <div className="flex min-h-0 flex-col">
      <p className="px-inset py-1.5 font-mono text-meta text-dim">
        {calls.length} calls, {total} ms in graph
      </p>
      <ol className="min-h-0 flex-1 overflow-y-auto">
        {calls.map((c, i) => {
          const isOpen = open === i;
          const all = c.cypher.join("\n\n");
          return (
            <li key={c.eventId} className="border-t border-rule/70">
              <button
                type="button"
                aria-expanded={isOpen}
                onClick={() => setOpen(isOpen ? null : i)}
                className="grid w-full grid-cols-[52px_minmax(0,1fr)_60px] items-baseline gap-3 sm:grid-cols-[52px_190px_minmax(0,1fr)_70px_60px] px-inset py-1.5 text-left font-mono text-label hover:bg-raise"
              >
                <span className="text-faint">+{(((c.ts - (t0 ?? c.ts)) / 1000)).toFixed(1)}s</span>
                <span className="truncate text-ink">{c.tool}</span>
                <span className="hidden truncate text-dim sm:block">{c.cypher[0]?.split("\n")[0]}</span>
                <span className="hidden text-right text-dim sm:block">{c.nodes_touched.length} nodes</span>
                <span className="text-right text-symptom">{c.ms} ms</span>
              </button>
              {isOpen && (
                <div className="space-y-2 px-inset pb-3">
                  <div className="flex items-center justify-between text-meta text-dim">
                    <span className="font-mono">graph {c.graph}</span>
                    <CopyButton text={all} label="Copy Cypher" />
                  </div>
                  {c.cypher.map((q, k) => (
                    <Cypher key={k} text={q} params={c.params[k] ?? c.params[0]} />
                  ))}
                  {c.nodes_touched.length > 0 && (
                    <p className="font-mono text-meta text-dim">
                      touched: {c.nodes_touched.slice(0, 12).join(", ")}
                      {c.nodes_touched.length > 12 && ` +${c.nodes_touched.length - 12}`}
                    </p>
                  )}
                </div>
              )}
            </li>
          );
        })}
      </ol>
    </div>
  );
}
