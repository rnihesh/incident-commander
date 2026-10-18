"use client";

import dynamic from "next/dynamic";
import type { GraphCanvasProps } from "./GraphCanvasInner";

// react-force-graph-2d touches window at import time; load it on the client only.
const Inner = dynamic(() => import("./GraphCanvasInner"), {
  ssr: false,
  loading: () => <div className="grid h-full place-items-center text-label text-faint">Loading the graph</div>,
});

const LEGEND: { key: string; label: string; swatch: React.ReactNode }[] = [
  { key: "alerting", label: "Alerting service", swatch: <i className="inline-block size-2.5 rounded-full bg-alert ring-2 ring-falsified/60" /> },
  { key: "symptomatic", label: "Symptomatic", swatch: <i className="inline-block size-2.5 rounded-full bg-symptom" /> },
  { key: "suspect", label: "Suspect", swatch: <i className="inline-block size-2.5 rounded-full border-2 border-drifted" /> },
  { key: "eliminated", label: "Eliminated (hover: failing clause)", swatch: <i className="inline-block size-2 rounded-full bg-node-out ring-1 ring-node-out-mark" /> },
  { key: "path", label: "Causal-path hop", swatch: <i className="inline-block size-2.5 rounded-full bg-verified" /> },
  { key: "late", label: "Late-arriving", swatch: <i className="inline-block size-2.5 rounded-full border-2 border-dashed border-late" /> },
  { key: "falsified", label: "FALSIFIED / new node", swatch: <i className="inline-block size-2.5 rounded-full border-2 border-falsified" /> },
];
const MAP_LEGEND: typeof LEGEND = [
  { key: "service", label: "Service (hover for its name)", swatch: <i className="inline-block size-2 rounded-full bg-node" /> },
  { key: "selected", label: "Selected target", swatch: <i className="inline-block size-2.5 rounded-full border-2 border-drifted" /> },
  { key: "deps", label: "Its dependencies", swatch: <i className="inline-block h-0.5 w-3 bg-drifted/70" /> },
];
const CHOKEPOINT = { key: "chokepoint", label: "Chokepoint (betweenness)", swatch: <i className="inline-block size-2.5 rotate-45 border-2 border-tampered" /> };

export function GraphCanvas(props: GraphCanvasProps) {
  return (
    <div className="flex h-full min-h-0 w-full flex-col">
      <div className="relative min-h-0 flex-1">
        <div className="absolute inset-0">
          <Inner {...props} />
        </div>
      </div>
      <ul className="flex flex-wrap gap-x-4 gap-y-1 border-t border-rule/60 px-3 py-2 text-meta text-dim" aria-label="Legend">
        {(props.variant === "map" ? MAP_LEGEND : props.graph.chokepoints?.length ? [...LEGEND, CHOKEPOINT] : LEGEND).map((l) => (
          <li key={l.key} className="flex items-center gap-1.5 whitespace-nowrap">
            {l.swatch}
            {l.label}
          </li>
        ))}
      </ul>
    </div>
  );
}
