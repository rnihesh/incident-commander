"use client";

import ForceGraph2D, { type ForceGraphMethods, type LinkObject, type NodeObject } from "react-force-graph-2d";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { GraphView } from "@/lib/reducer";
import { roleOf, ROLE_LABEL } from "@/lib/roles";
import { labelRank, placeLabels, type LabelCandidate } from "@/lib/labels";
import { labelAwareFit } from "@/lib/fit";
import type { GraphEdge, GraphNode } from "@/lib/mock/hero-graph";
import { subscribeTheme } from "@/lib/theme";

export interface GraphCanvasProps {
  nodes: GraphNode[];
  edges: GraphEdge[];
  graph: GraphView;
  view: "true" | "known";
  focus: { ids: string[]; n: number } | null;
  onNodeClick?: (id: string) => void;
  /** True once the run has settled (verdict or end of stream): the view refits once then. */
  settled?: boolean;
}

type NodeData = { id: string; name: string; fx: number; fy: number };
type N = NodeObject<NodeData>;
type L = LinkObject<NodeData>;

/** Canvas cannot read CSS variables, so the palette is resolved from the theme tokens in
 *  globals.css once on the client. Keys are graph roles plus the few canvas-only marks. */
function readPalette() {
  const css = getComputedStyle(document.documentElement);
  const v = (name: string) => css.getPropertyValue(`--color-${name}`).trim();
  return {
    plain: v("node"),
    eliminated: v("node-out"),
    eliminatedMark: v("node-out-mark"),
    symptomatic: v("symptom"),
    suspect: v("drifted"),
    path: v("verified"),
    pathParticle: v("verified-hi"),
    alerting: v("alert"),
    late: v("late"),
    falsified: v("falsified"),
    chokepoint: v("tampered"),
    deck: v("deck"),
    ink: v("ink"),
    dim: v("dim"),
    edge: v("dim"),
    // next/font exposes the mono family as a CSS variable; the canvas needs the resolved list.
    font: getComputedStyle(document.body).getPropertyValue("--font-data").trim() || "ui-monospace, monospace",
  };
}
type Palette = ReturnType<typeof readPalette>;

/** `#rrggbb` plus alpha as an rgba() string for canvas strokes. */
function alpha(hex: string, a: number) {
  let h = hex.replace("#", "");
  // Computed tokens may come back in the short form (#fff): expand it.
  if (h.length === 3) h = h.replace(/./g, (c) => c + c);
  const n = parseInt(h.slice(0, 6), 16);
  return `rgba(${(n >> 16) & 255},${(n >> 8) & 255},${n & 255},${a})`;
}

const BASE_R = { plain: 4, eliminated: 3.4, symptomatic: 6, suspect: 7, path: 7, alerting: 9 } as const;

/** Node radius in graph units at zoom k. Zooming in past ~1.5x stops growing the dots (a fitted
 *  dense graph would otherwise turn into overlapping discs); highlighted roles may grow a bit more. */
function nodeRadius(role: keyof typeof BASE_R, k: number) {
  const cap = role === "plain" || role === "eliminated" ? 1.5 : 2.2;
  return BASE_R[role] * Math.min(1, cap / Math.max(k, 1e-6));
}

/** Screen pixels kept clear around a fitted graph. */
const FIT_PAD = 20;
/** Advance of one label character: 11px JetBrains Mono is 0.6em wide. */
const LABEL_CH_W = 6.6;
/** How long a focused node stays zoomed and ringed before the view refits. */
const FOCUS_HOLD_MS = 2600;

const endId = (e: unknown) => (typeof e === "object" && e ? (e as { id: string }).id : (e as string));

export default function GraphCanvasInner({ nodes, edges, graph, view, focus, onNodeClick, settled = false }: GraphCanvasProps) {
  const fg = useRef<ForceGraphMethods<N, L> | undefined>(undefined);
  const box = useRef<HTMLDivElement>(null);
  const [size, setSize] = useState({ w: 800, h: 480 });
  const sizeRef = useRef(size);
  useEffect(() => {
    sizeRef.current = size;
  }, [size]);
  const [hover, setHover] = useState<{ id: string; x: number; y: number } | null>(null);
  const pal = useRef<Palette | null>(null);
  const colors = () => (pal.current ??= readPalette());
  // The canvas redraws every frame (autoPauseRedraw off), so dropping the cached palette on a
  // theme switch is enough for the next frame to paint in the new colours.
  useEffect(() => subscribeTheme(() => { pal.current = null; }), []);

  // Highlights live in refs; graphData is built once so the layout never moves or restarts.
  const graphRef = useRef(graph);
  const viewRef = useRef(view);
  const focusRef = useRef<Set<string>>(new Set());
  const hoverRef = useRef<string | null>(null);
  const labelsRef = useRef<(LabelCandidate & { name: string; dim: boolean })[]>([]);
  useEffect(() => {
    graphRef.current = graph;
    viewRef.current = view;
  }, [graph, view]);

  // Large worlds (the judge's whole service map) only label plain nodes on hover.
  const crowdedRef = useRef(false);
  useEffect(() => {
    crowdedRef.current = nodes.length > 150;
  }, [nodes]);

  const data = useMemo(
    () => ({
      nodes: nodes.map((n) => ({ id: n.id, name: n.name, x: n.x, y: n.y, fx: n.x, fy: n.y })),
      links: edges.map((e) => ({ source: e.source, target: e.target })),
    }),
    [nodes, edges],
  );

  const pathKeys = useMemo(() => {
    const s = new Set<string>();
    graph.paths.forEach((p) => p.nodes.forEach((n, i) => {
        if (!i) return;
        s.add(`${p.nodes[i - 1]}>${n}`);
        s.add(`${n}>${p.nodes[i - 1]}`); // tolerate edges stored consumer to cause
      }));
    return s;
  }, [graph.paths]);
  const pathKeysRef = useRef(pathKeys);
  useEffect(() => {
    pathKeysRef.current = pathKeys;
  }, [pathKeys]);

  useEffect(() => {
    const el = box.current;
    if (!el) return;
    const ro = new ResizeObserver(() => {
      setSize({ w: Math.max(200, el.clientWidth), h: Math.max(200, el.clientHeight) });
    });
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  // Fit the bulk of the graph, not its stragglers: a couple of far-out plain nodes would
  // otherwise shrink the whole picture to a dot. Highlighted nodes are always kept in frame.
  const fitFilter = useMemo(() => {
    const q = (xs: number[], p: number) => xs[Math.min(xs.length - 1, Math.max(0, Math.round(p * (xs.length - 1))))];
    const xs = data.nodes.map((n) => n.x).sort((a, b) => a - b);
    const ys = data.nodes.map((n) => n.y).sort((a, b) => a - b);
    const trim = data.nodes.length >= 20 ? 0.03 : 0;
    // Quantile box grown by a fifth of its size: an evenly spread graph keeps every node,
    // only nodes far outside the bulk fall away.
    const [x0, x1, y0, y1] = [q(xs, trim), q(xs, 1 - trim), q(ys, trim), q(ys, 1 - trim)];
    const px = (x1 - x0) * 0.2;
    const py = (y1 - y0) * 0.2;
    const box = { x0: x0 - px, x1: x1 + px, y0: y0 - py, y1: y1 + py };
    return (n: N) => {
      const x = n.x ?? 0;
      const y = n.y ?? 0;
      if (x >= box.x0 && x <= box.x1 && y >= box.y0 && y <= box.y1) return true;
      return roleOf(n.id as string, graphRef.current) !== "plain";
    };
  }, [data]);

  // Frame the fitted nodes with their labels inside the canvas. Highlighted roles are always
  // labelled; plain ones too once zoomed past 3x on a small graph (see paintNode), so a fit that
  // lands above 3x is redone with every label. Falls back to a plain node fit when the labels
  // alone are wider than the canvas.
  const fitView = useCallback(
    (ms: number) => {
      const g = graphRef.current;
      const nodes = data.nodes.filter((n) => fitFilter(n as N) && !(viewRef.current === "known" && g.late.includes(n.id)));
      const pts = (all: boolean) =>
        nodes.map((n) => {
          const role = roleOf(n.id, g);
          const labelled = all || (role !== "plain" && role !== "eliminated");
          return { x: n.x, y: n.y, lw: labelled ? n.name.length * LABEL_CH_W : 0 };
        });
      const opts = { pad: FIT_PAD, r: 10, lh: 16, kMax: 4 };
      let f = labelAwareFit(pts(false), size.w, size.h, opts);
      if (f && f.k > 3 && !crowdedRef.current) {
        const all = labelAwareFit(pts(true), size.w, size.h, opts);
        f = all && all.k > 3 ? all : labelAwareFit(pts(false), size.w, size.h, { ...opts, kMax: 3 });
      }
      if (!f) return void fg.current?.zoomToFit(ms, FIT_PAD, fitFilter);
      fg.current?.centerAt(f.cx, f.cy, ms);
      fg.current?.zoom(f.k, ms);
    },
    [data, fitFilter, size],
  );
  // Effects below read the latest fit through a ref, so a resize never replays a focus.
  const fitRef = useRef(fitView);
  useEffect(() => {
    fitRef.current = fitView;
  }, [fitView]);

  // The canvas stays hidden until its first fit, so a new graph never flashes at the default zoom.
  const [fitted, setFitted] = useState(false);
  useEffect(() => {
    const t = setTimeout(() => {
      fitView(0);
      setFitted(true);
    }, 250);
    return () => clearTimeout(t);
  }, [fitView]);

  // Settling changes which nodes are highlighted (and so kept in frame): refit once, animated.
  useEffect(() => {
    if (!settled) return;
    const t = setTimeout(() => fitRef.current(500), 300);
    return () => clearTimeout(t);
  }, [settled]);

  useEffect(() => {
    if (!focus) {
      // Focus cleared while a node was still held: drop the ring and refit now.
      if (focusRef.current.size) {
        focusRef.current = new Set();
        fitRef.current(600);
      }
      return;
    }
    focusRef.current = new Set(focus.ids);
    const pts = data.nodes.filter((n) => focus.ids.includes(n.id));
    if (!pts.length) {
      fitRef.current(500);
      return;
    }
    const cx = pts.reduce((a, p) => a + p.x, 0) / pts.length;
    const cy = pts.reduce((a, p) => a + p.y, 0) / pts.length;
    fg.current?.centerAt(cx, cy, 500);
    fg.current?.zoom(pts.length > 1 ? 1.6 : 2.4, 500);
    // Once the focus ring fades, ease back to the whole graph.
    const t = setTimeout(() => {
      focusRef.current = new Set();
      fitRef.current(600);
    }, FOCUS_HOLD_MS);
    return () => clearTimeout(t);
  }, [focus, data]);

  const hidden = useCallback((id: string) => viewRef.current === "known" && graphRef.current.late.includes(id), []);

  const paintNode = useCallback((node: N, ctx: CanvasRenderingContext2D, k: number) => {
    const id = node.id as string;
    if (hidden(id)) return;
    const g = graphRef.current;
    const role = roleOf(id, g);
    const x = node.x ?? 0;
    const y = node.y ?? 0;
    const r = nodeRadius(role, k);
    const now = performance.now();
    const C = colors();

    if (role === "alerting") {
      const pulse = 0.5 + 0.5 * Math.sin(now / 420);
      ctx.beginPath();
      ctx.arc(x, y, r + 5 + pulse * 4, 0, 6.283);
      ctx.strokeStyle = alpha(C.falsified, 0.25 + pulse * 0.4);
      ctx.lineWidth = 1.5;
      ctx.stroke();
    }
    ctx.beginPath();
    ctx.arc(x, y, r, 0, 6.283);
    if (role === "suspect") {
      ctx.fillStyle = C.deck;
      ctx.fill();
      ctx.lineWidth = 2;
      ctx.strokeStyle = C.suspect;
      ctx.stroke();
    } else {
      ctx.fillStyle = C[role];
      ctx.fill();
      if (role === "symptomatic") {
        ctx.beginPath();
        ctx.arc(x, y, r + 2.5, 0, 6.283);
        ctx.strokeStyle = alpha(C.symptomatic, 0.35);
        ctx.lineWidth = 1;
        ctx.stroke();
      }
    }
    if (role === "eliminated") {
      ctx.strokeStyle = C.eliminatedMark;
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(x - 2, y - 2);
      ctx.lineTo(x + 2, y + 2);
      ctx.moveTo(x + 2, y - 2);
      ctx.lineTo(x - 2, y + 2);
      ctx.stroke();
    }
    if (viewRef.current === "true" && g.late.includes(id)) {
      ctx.beginPath();
      ctx.setLineDash([2.5, 2.5]);
      ctx.arc(x, y, r + 4.5, 0, 6.283);
      ctx.strokeStyle = C.late;
      ctx.lineWidth = 1.4;
      ctx.stroke();
      ctx.setLineDash([]);
    }
    if (g.falsified.includes(id)) {
      ctx.beginPath();
      ctx.arc(x, y, r + 5, 0, 6.283);
      ctx.strokeStyle = C.falsified;
      ctx.lineWidth = 2;
      ctx.stroke();
    }
    if (g.chokepoints?.includes(id)) {
      // Storm chokepoint (seeded betweenness): a diamond outline around the node.
      const d = r + 6;
      ctx.beginPath();
      ctx.moveTo(x, y - d);
      ctx.lineTo(x + d, y);
      ctx.lineTo(x, y + d);
      ctx.lineTo(x - d, y);
      ctx.closePath();
      ctx.strokeStyle = C.chokepoint;
      ctx.lineWidth = 1.6;
      ctx.stroke();
    }
    if (focusRef.current.has(id)) {
      ctx.beginPath();
      ctx.arc(x, y, r + 8, 0, 6.283);
      ctx.strokeStyle = alpha(C.alerting, 0.9);
      ctx.lineWidth = 1.5;
      ctx.stroke();
    }
    const showLabel = role !== "plain" && role !== "eliminated" ? true : (k > 3 && !crowdedRef.current) || hoverRef.current === id;
    if (showLabel) {
      // Labels are queued here and drawn after every node, so they can avoid each other.
      ctx.font = `500 ${11 / k}px ${C.font}`;
      const name = node.name as string;
      // Clear the outermost ring drawn above, so a FALSIFIED or alert ring never strikes the text.
      const ring = Math.max(
        role === "alerting" ? r + 7 : role === "symptomatic" ? r + 2.5 : r,
        viewRef.current === "true" && g.late.includes(id) ? r + 4.5 : 0,
        g.falsified.includes(id) ? r + 5 : 0,
        g.chokepoints?.includes(id) ? r + 6 : 0,
      );
      labelsRef.current.push({
        id, x, y, r: ring, w: ctx.measureText(name).width, h: 11 / k,
        rank: hoverRef.current === id ? -1 : labelRank(id, role, g),
        name, dim: role === "plain" || role === "eliminated",
      });
    }
  }, [hidden]);

  const clearLabels = useCallback(() => {
    labelsRef.current = [];
  }, []);

  // 11 screen pixels at any zoom, on a dark halo so labels stay legible over edges. Overlapping
  // labels move to a free side of their node or are skipped, highest-priority role first.
  const drawLabels = useCallback((ctx: CanvasRenderingContext2D, k: number) => {
    const C = colors();
    const queued = new Map(labelsRef.current.map((l) => [l.id, l]));
    ctx.font = `500 ${11 / k}px ${C.font}`;
    ctx.textAlign = "center";
    ctx.textBaseline = "top";
    ctx.lineJoin = "round";
    ctx.lineWidth = 3 / k;
    // The visible canvas in graph units, so edge labels flip inward instead of running off it.
    const a = fg.current?.screen2GraphCoords(0, 0);
    const b = fg.current?.screen2GraphCoords(sizeRef.current.w, sizeRef.current.h);
    const view = a && b ? { x0: a.x, y0: a.y, x1: b.x, y1: b.y } : undefined;
    for (const p of placeLabels(labelsRef.current, 3 / k, view)) {
      const l = queued.get(p.id)!;
      ctx.strokeStyle = alpha(C.deck, 0.85);
      ctx.strokeText(l.name, p.tx, p.ty);
      ctx.fillStyle = l.dim ? C.dim : C.ink;
      ctx.fillText(l.name, p.tx, p.ty);
    }
  }, []);

  const paintPointer = useCallback((node: N, color: string, ctx: CanvasRenderingContext2D, k: number) => {
    if (hidden(node.id as string)) return;
    ctx.fillStyle = color;
    ctx.beginPath();
    ctx.arc(node.x ?? 0, node.y ?? 0, nodeRadius(roleOf(node.id as string, graphRef.current), k) + 4 / k, 0, 6.283);
    ctx.fill();
  }, [hidden]);

  const linkOnPath = useCallback((l: L) => pathKeysRef.current.has(`${endId(l.source)}>${endId(l.target)}`), []);
  const linkColor = useCallback((l: L) => {
    if (linkOnPath(l)) return colors().path;
    const g = graphRef.current;
    const a = endId(l.source) as string;
    const b = endId(l.target) as string;
    if (g.symptomatic.includes(a) && g.symptomatic.includes(b)) return alpha(colors().symptomatic, 0.45);
    return alpha(colors().edge, 0.18);
  }, [linkOnPath]);
  const linkWidth = useCallback((l: L) => (linkOnPath(l) ? 2.4 : 0.7), [linkOnPath]);
  const linkVisible = useCallback((l: L) => !hidden(endId(l.source) as string) && !hidden(endId(l.target) as string), [hidden]);
  // New function identity only when the path changes, which makes the library re-seed particles.
  const particles = useMemo(() => {
    const keys = pathKeys;
    return (l: L) => (keys.has(`${endId(l.source)}>${endId(l.target)}`) ? 3 : 0);
  }, [pathKeys]);

  const hovered = hover ? { role: roleOf(hover.id, graph), clause: graph.eliminated[hover.id] } : null;

  return (
    <div ref={box} className={`relative h-full w-full overflow-hidden transition-opacity duration-200 ${fitted ? "opacity-100" : "opacity-0"}`} role="img" aria-label="Service graph. A text list of the causal path is beside the canvas.">
      <ForceGraph2D<NodeData>
        ref={fg}
        width={size.w}
        height={size.h}
        graphData={data}
        backgroundColor="rgba(0,0,0,0)"
        cooldownTicks={0}
        warmupTicks={0}
        autoPauseRedraw={false}
        enableNodeDrag={false}
        nodeRelSize={4}
        nodeCanvasObjectMode={() => "replace"}
        nodeCanvasObject={paintNode as never}
        nodePointerAreaPaint={paintPointer as never}
        onRenderFramePre={clearLabels}
        onRenderFramePost={drawLabels}
        linkColor={linkColor as never}
        linkWidth={linkWidth as never}
        linkVisibility={linkVisible as never}
        linkDirectionalArrowLength={3}
        linkDirectionalArrowRelPos={0.92}
        linkDirectionalParticles={particles as never}
        linkDirectionalParticleWidth={3}
        linkDirectionalParticleSpeed={0.007}
        linkDirectionalParticleColor={() => colors().pathParticle}
        onNodeHover={(n) => {
          hoverRef.current = n ? (n.id as string) : null;
          if (!n || !fg.current) return setHover(null);
          const p = fg.current.graph2ScreenCoords(n.x ?? 0, n.y ?? 0);
          setHover({ id: n.id as string, x: p.x, y: p.y });
        }}
        onNodeClick={(n) => onNodeClick?.(n.id as string)}
      />
      {hover && hovered && (
        <div
          className="pointer-events-none absolute z-10 max-w-64 rounded-ctl border border-rule-hi bg-deck/95 px-2.5 py-1.5 font-mono text-meta shadow-lg"
          style={{ left: Math.min(hover.x + 12, size.w - 230), top: Math.max(hover.y - 8, 4) }}
        >
          <div className="text-ink">{hover.id}</div>
          <div className="text-dim">{ROLE_LABEL[hovered.role]}</div>
          {hovered.clause && (
            <div className="mt-1 text-drifted">
              eliminated by {hovered.clause.clause} ({hovered.clause.change})
            </div>
          )}
        </div>
      )}
    </div>
  );
}
