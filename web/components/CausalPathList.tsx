"use client";

import type { GraphView } from "@/lib/reducer";
import { shortSvc, shortSvcs } from "./ui";

/** Text equivalent of the canvas highlights, for screen readers and for reading the path at a glance. */
export function CausalPathList({ graph, onFocus }: { graph: GraphView; onFocus: (ids: string[]) => void }) {
  return (
    <aside aria-label="Causal path, text" className="flex min-h-0 flex-col overflow-y-auto border-t border-rule bg-raise/40 px-4 py-3 text-label sm:border-l sm:border-t-0">
      <h3 className="text-body font-semibold">Causal path</h3>
      <p className="mt-0.5 text-label text-dim">How the failure spread from the changed service to the one that alerted.</p>
      {graph.paths.length === 0 ? (
        <p className="mt-2 text-label text-faint">The path appears once a suspect qualifies.</p>
      ) : (
        graph.paths.map((p) => (
          <div key={p.label} className="mt-2">
            {graph.paths.length > 1 && <p className="text-meta text-dim">{p.label}</p>}
            <ol className="mt-1.5 ml-[3px] border-l border-verified/60">
              {p.nodes.map((n, i) => (
                <li key={n}>
                  <button type="button" onClick={() => onFocus([n])} title={n} className="group relative flex w-full flex-wrap items-baseline gap-x-2 py-1 pl-3.5 text-left">
                    <span className="absolute -left-1 top-[9px] size-[7px] rounded-full bg-verified" aria-hidden />
                    <span className="max-w-full truncate whitespace-nowrap font-mono text-ink group-hover:text-symptom">{shortSvc(n)}</span>
                    <span className="text-meta text-faint">{i === 0 ? "origin" : i === p.nodes.length - 1 ? "alerting" : `hop ${i}`}</span>
                  </button>
                </li>
              ))}
            </ol>
          </div>
        ))
      )}
      {graph.symptomatic.length > 0 && (
        <div className="mt-4">
          <p className="text-meta text-dim">Symptomatic ({graph.symptomatic.length})</p>
          <p className="mt-1 font-mono text-meta leading-relaxed text-ink/80">
            {shortSvcs(graph.symptomatic).map((n, i, all) => (
              <span key={n} className="whitespace-nowrap">{n}{i < all.length - 1 ? "," : ""}</span>
            )).reduce<React.ReactNode[]>((acc, el, i) => (i ? [...acc, " ", el] : [el]), [])}
          </p>
        </div>
      )}
      {(graph.late.length > 0 || (graph.lateEdges?.length ?? 0) > 0) && (
        <p className="mt-3 text-meta text-late">
          Late-arriving:{" "}
          <span className="font-mono">
            {[...graph.late.map(shortSvc), ...(graph.lateEdges ?? []).map((e) => `${shortSvc(e.caller)} → ${shortSvc(e.callee)}`)].join(", ")}
          </span>
        </p>
      )}
    </aside>
  );
}
