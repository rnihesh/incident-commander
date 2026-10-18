"use client";

import ForceGraph2D, { type ForceGraphMethods, type LinkObject, type NodeObject } from "react-force-graph-2d";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { GraphView } from "@/lib/reducer";
import { roleOf, ROLE_LABEL } from "@/lib/roles";
import { labelNames, labelRank, placeLabels, type LabelCandidate } from "@/lib/labels";
import { labelAwareFit } from "@/lib/fit";
import type { Caption } from "@/lib/graphLayout";
import type { GraphEdge, GraphNode, NodeRole } from "@/lib/mock/hero-graph";
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
  /** "map" is the judge's whole service map: edges only around the selected or hovered
   *  service, names on hover, region and domain captions. Default "run". */
  variant?: "run" | "map";
  captions?: Caption[];
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
    rule: v("rule-hi"),
    ink: v("ink"),
    dim: v("dim"),
    faint: v("faint"),
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

/** Node radius in screen pixels at zoom 1, by role: a clear size step from background to alert. */
const SCREEN_R: Record<NodeRole, number> = { plain: 3.2, eliminated: 4, symptomatic: 7, suspect: 8, path: 8, alerting: 10 };

/** Node radius in graph units at zoom k. Dots keep roughly their screen size: they shrink a
 *  little when zoomed far out (a dense map) and grow a little when zoomed in, never more. */
function nodeRadius(role: NodeRole, k: number) {
  const kk = Math.max(k, 1e-6);
  return (SCREEN_R[role] * Math.min(1.25, Math.max(0.75, kk))) / kk;
}

const isKey = (role: NodeRole) => role !== "plain" && role !== "eliminated";

/** Screen pixels kept clear around a fitted graph. */
const FIT_PAD = 28;
/** Label font size in screen pixels, and the pill padding around the text. */
const LABEL_PX = 12;
const PILL_X = 4;
const PILL_Y = 2;
/** Advance of one label character: JetBrains Mono is 0.6em wide. */
const LABEL_CH_W = LABEL_PX * 0.6;
/** How long a focused node stays zoomed and ringed before the view refits. */
const FOCUS_HOLD_MS = 2600;

const endId = (e: unknown) => (typeof e === "object" && e ? (e as { id: string }).id : (e as string));

export default function GraphCanvasInner({ nodes, edges, graph, view, focus, onNodeClick, settled = false, variant = "run", captions }: GraphCanvasProps) {
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
  const isMap = variant === "map";
  const mapRef = useRef(isMap);
  const captionsRef = useRef(captions ?? []);
  useEffect(() => {
    mapRef.current = isMap;
    captionsRef.current = captions ?? [];
  }, [isMap, captions]);

  // Large worlds (the judge's whole service map) only label plain nodes on hover.
  const crowdedRef = useRef(false);
  useEffect(() => {
    crowdedRef.current = isMap || nodes.length > 150;
  }, [nodes, isMap]);

  const data = useMemo(
    () => ({
      nodes: nodes.map((n) => ({ id: n.id, name: n.name, x: n.x, y: n.y, fx: n.x, fy: n.y })),
      links: edges.map((e) => ({ source: e.source, target: e.target })),
    }),
    [nodes, edges],
  );
  const names = useMemo(() => labelNames(nodes), [nodes]);
  const namesRef = useRef(names);
  useEffect(() => {
    namesRef.current = names;
  }, [names]);
  // Neighbours of each node, for fading the background of a run to its incident subgraph.
  const adj = useMemo(() => {
    const m = new Map<string, string[]>();
    for (const e of edges) {
      (m.get(e.source) ?? m.set(e.source, []).get(e.source)!).push(e.target);
      (m.get(e.target) ?? m.set(e.target, []).get(e.target)!).push(e.source);
    }
    return m;
  }, [edges]);

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

  // Run graphs: plain services more than one hop from every highlighted one are drawn as faint
  // specks, so the incident subgraph stands out. Recomputed when the roles change.
  const near = useMemo(() => {
    const s = new Set<string>();
    if (isMap) return s;
    for (const n of nodes) {
      if (roleOf(n.id, graph) === "plain") continue;
      s.add(n.id);
      for (const m of adj.get(n.id) ?? []) s.add(m);
    }
    return s;
  }, [nodes, graph, adj, isMap]);
  const nearRef = useRef(near);
  useEffect(() => {
    nearRef.current = near;
  }, [near]);

  useEffect(() => {
    const el = box.current;
    if (!el) return;
    const ro = new ResizeObserver(() => {
      setSize({ w: Math.max(200, el.clientWidth), h: Math.max(200, el.clientHeight) });
    });
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  // What the fit frames. A run frames its incident subgraph (alerting, symptomatic, suspects,
  // causal path) when it has at least two such nodes; the map, or a run with fewer, frames the
  // bulk of the graph without its far stragglers (highlighted nodes always stay in frame).
  const makeFitFilter = useCallback(() => {
    const q = (xs: number[], p: number) => xs[Math.min(xs.length - 1, Math.max(0, Math.round(p * (xs.length - 1))))];
    const xs = data.nodes.map((n) => n.x).sort((a, b) => a - b);
    const ys = data.nodes.map((n) => n.y).sort((a, b) => a - b);
    const trim = data.nodes.length >= 20 && !isMap ? 0.03 : 0;
    const [x0, x1, y0, y1] = [q(xs, trim), q(xs, 1 - trim), q(ys, trim), q(ys, 1 - trim)];
    const px = (x1 - x0) * 0.2;
    const py = (y1 - y0) * 0.2;
    const bulk = { x0: x0 - px, x1: x1 + px, y0: y0 - py, y1: y1 + py };
    const g = graphRef.current;
    const key = (id: string) => isKey(roleOf(id, g)) || g.falsified.includes(id) || !!g.chokepoints?.includes(id);
    const subgraph = !isMap && data.nodes.filter((m) => key(m.id)).length >= 2;
    return (n: N) => {
      if (subgraph) return key(n.id as string);
      const x = n.x ?? 0;
      const y = n.y ?? 0;
      if (x >= bulk.x0 && x <= bulk.x1 && y >= bulk.y0 && y <= bulk.y1) return true;
      return roleOf(n.id as string, g) !== "plain";
    };
  }, [data, isMap]);

  // Frame the fitted nodes with their labels inside the canvas. Highlighted roles are always
  // labelled; plain ones too once zoomed past 3x on a small graph (see paintNode), so a fit that
  // lands above 3x is redone with every label. Falls back to a plain node fit when the labels
  // alone are wider than the canvas.
  const fitView = useCallback(
    (ms: number) => {
      const g = graphRef.current;
      const fitFilter = makeFitFilter();
      const shown = data.nodes.filter((n) => fitFilter(n as N) && !(viewRef.current === "known" && g.late.includes(n.id)));
      const pillW = (id: string) => (namesRef.current.get(id) ?? id).length * LABEL_CH_W + 2 * PILL_X;
      const pts = (all: boolean) => [
        ...shown.map((n) => ({ x: n.x, y: n.y, lw: all || isKey(roleOf(n.id, g)) ? pillW(n.id) : 0 })),
        ...(mapRef.current ? captionsRef.current.map((c) => ({ x: c.x, y: c.y, lw: c.text.length * 8 })) : []),
      ];
      const opts = { pad: FIT_PAD, r: 14, lh: LABEL_PX + 2 * PILL_Y + 4, kMax: mapRef.current ? 3 : 2.4 };
      let f = labelAwareFit(pts(false), size.w, size.h, opts);
      if (f && f.k > 3 && !crowdedRef.current) {
        const all = labelAwareFit(pts(true), size.w, size.h, opts);
        f = all && all.k > 3 ? all : labelAwareFit(pts(false), size.w, size.h, { ...opts, kMax: 3 });
      }
      if (!f) return void fg.current?.zoomToFit(ms, FIT_PAD, fitFilter);
      fg.current?.centerAt(f.cx, f.cy, ms);
      fg.current?.zoom(f.k, ms);
    },
    [data, makeFitFilter, size],
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
    fg.current?.zoom(pts.length > 1 ? 1.6 : 2.2, 500);
    // Once the focus ring fades, ease back to the whole graph.
    const t = setTimeout(() => {
      focusRef.current = new Set();
      fitRef.current(600);
    }, FOCUS_HOLD_MS);
    return () => clearTimeout(t);
  }, [focus, data]);

  const hidden = useCallback((id: string) => viewRef.current === "known" && graphRef.current.late.includes(id), []);
  // A background speck: a plain run node away from the incident subgraph.
  const faded = useCallback((id: string) => !mapRef.current && nearRef.current.size > 0 && !nearRef.current.has(id), []);

  const paintNode = useCallback((node: N, ctx: CanvasRenderingContext2D, k: number) => {
    const id = node.id as string;
    if (hidden(id)) return;
    const g = graphRef.current;
    const role = roleOf(id, g);
    const x = node.x ?? 0;
    const y = node.y ?? 0;
    const r = nodeRadius(role, k);
    const u = 1 / k; // one screen pixel in graph units: rings and strokes keep their screen size
    const now = performance.now();
    const C = colors();
    const speck = role === "plain" && faded(id) && hoverRef.current !== id;

    if (role === "alerting") {
      const pulse = 0.5 + 0.5 * Math.sin(now / 420);
      ctx.beginPath();
      ctx.arc(x, y, r + (5 + pulse * 4) * u, 0, 6.283);
      ctx.strokeStyle = alpha(C.falsified, 0.25 + pulse * 0.4);
      ctx.lineWidth = 1.5 * u;
      ctx.stroke();
    }
    if (role === "suspect" && mapRef.current) {
      // The selected target on the dense map: a soft halo so it is found at a glance.
      ctx.beginPath();
      ctx.arc(x, y, r + 7 * u, 0, 6.283);
      ctx.fillStyle = alpha(C.suspect, 0.22);
      ctx.fill();
    }
    ctx.beginPath();
    ctx.arc(x, y, speck ? r * 0.75 : r, 0, 6.283);
    if (role === "suspect") {
      ctx.fillStyle = C.deck;
      ctx.fill();
      ctx.lineWidth = 2.5 * u;
      ctx.strokeStyle = C.suspect;
      ctx.stroke();
    } else {
      ctx.fillStyle = speck ? alpha(C.plain, 0.45) : C[role];
      ctx.fill();
      if (role === "symptomatic") {
        ctx.beginPath();
        ctx.arc(x, y, r + 3 * u, 0, 6.283);
        ctx.strokeStyle = alpha(C.symptomatic, 0.4);
        ctx.lineWidth = 1.2 * u;
        ctx.stroke();
      }
    }
    // The verdict's root (a suspect drawn as a path hop) keeps a suspect ring around the hop.
    if (role === "path" && g.suspects.includes(id)) {
      ctx.beginPath();
      ctx.arc(x, y, r + 3.5 * u, 0, 6.283);
      ctx.strokeStyle = C.suspect;
      ctx.lineWidth = 2 * u;
      ctx.stroke();
    }
    if (role === "eliminated") {
      const m = r * 0.55;
      ctx.strokeStyle = C.eliminatedMark;
      ctx.lineWidth = 1 * u;
      ctx.beginPath();
      ctx.moveTo(x - m, y - m);
      ctx.lineTo(x + m, y + m);
      ctx.moveTo(x + m, y - m);
      ctx.lineTo(x - m, y + m);
      ctx.stroke();
    }
    if (viewRef.current === "true" && g.late.includes(id)) {
      ctx.beginPath();
      ctx.setLineDash([2.5 * u, 2.5 * u]);
      ctx.arc(x, y, r + 5 * u, 0, 6.283);
      ctx.strokeStyle = C.late;
      ctx.lineWidth = 1.5 * u;
      ctx.stroke();
      ctx.setLineDash([]);
    }
    if (g.falsified.includes(id)) {
      ctx.beginPath();
      ctx.arc(x, y, r + 6 * u, 0, 6.283);
      ctx.strokeStyle = C.falsified;
      ctx.lineWidth = 2.5 * u;
      ctx.stroke();
    }
    if (g.chokepoints?.includes(id)) {
      // Storm chokepoint (seeded betweenness): a diamond outline around the node.
      const d = r + 6 * u;
      ctx.beginPath();
      ctx.moveTo(x, y - d);
      ctx.lineTo(x + d, y);
      ctx.lineTo(x, y + d);
      ctx.lineTo(x - d, y);
      ctx.closePath();
      ctx.strokeStyle = C.chokepoint;
      ctx.lineWidth = 1.6 * u;
      ctx.stroke();
    }
    if (focusRef.current.has(id)) {
      ctx.beginPath();
      ctx.arc(x, y, r + 9 * u, 0, 6.283);
      ctx.strokeStyle = alpha(C.alerting, 0.9);
      ctx.lineWidth = 1.5 * u;
      ctx.stroke();
    }
    const showLabel = isKey(role) ? true : (k > 3 && !crowdedRef.current) || hoverRef.current === id;
    if (showLabel) {
      // Labels are queued here and drawn after every node, so they can avoid each other.
      ctx.font = `500 ${LABEL_PX * u}px ${C.font}`;
      const name = namesRef.current.get(id) ?? (node.name as string);
      // Clear the outermost ring drawn above, so a FALSIFIED or alert ring never strikes the text.
      const ring = Math.max(
        role === "alerting" ? r + 9 * u : role === "symptomatic" ? r + 3 * u : r + 3.5 * u,
        viewRef.current === "true" && g.late.includes(id) ? r + 5 * u : 0,
        g.falsified.includes(id) ? r + 6 * u : 0,
        g.chokepoints?.includes(id) ? r + 6 * u : 0,
      );
      labelsRef.current.push({
        id, x, y, r: ring, w: ctx.measureText(name).width + 2 * PILL_X * u, h: (LABEL_PX + 2 * PILL_Y) * u,
        rank: hoverRef.current === id ? -1 : labelRank(id, role, g),
        name, dim: !isKey(role),
      });
    }
  }, [hidden, faded]);

  const clearLabels = useCallback(() => {
    labelsRef.current = [];
  }, []);

  // Labels: 12 screen pixels at any zoom, on a pill so they stay legible over edges and dots.
  // Overlapping labels move to a free side of their node or are skipped, highest-priority role
  // first (hover names any node). The map also draws its region and domain captions here.
  const drawLabels = useCallback((ctx: CanvasRenderingContext2D, k: number) => {
    const C = colors();
    const u = 1 / k;
    if (mapRef.current) {
      ctx.textAlign = "center";
      ctx.textBaseline = "middle";
      for (const c of captionsRef.current) {
        const region = c.kind === "region";
        ctx.font = `${region ? 600 : 500} ${(region ? 13 : 11) * u}px ${C.font}`;
        ctx.fillStyle = region ? C.ink : C.faint;
        ctx.fillText(region ? c.text : c.text.toUpperCase(), c.x, c.y);
      }
    }
    const queued = new Map(labelsRef.current.map((l) => [l.id, l]));
    ctx.font = `500 ${LABEL_PX * u}px ${C.font}`;
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    // The visible canvas in graph units, so edge labels flip inward instead of running off it.
    const a = fg.current?.screen2GraphCoords(0, 0);
    const b = fg.current?.screen2GraphCoords(sizeRef.current.w, sizeRef.current.h);
    const view = a && b ? { x0: a.x, y0: a.y, x1: b.x, y1: b.y } : undefined;
    for (const p of placeLabels(labelsRef.current, 3 * u, view)) {
      const l = queued.get(p.id)!;
      const { x0, y0, x1, y1 } = p.box;
      ctx.beginPath();
      ctx.roundRect(x0, y0, x1 - x0, y1 - y0, 4 * u);
      ctx.fillStyle = alpha(C.deck, 0.9);
      ctx.fill();
      ctx.strokeStyle = alpha(C.rule, l.dim ? 0.5 : 0.9);
      ctx.lineWidth = 1 * u;
      ctx.stroke();
      ctx.fillStyle = l.dim ? C.dim : C.ink;
      ctx.fillText(l.name, (x0 + x1) / 2, (y0 + y1) / 2 + 0.5 * u);
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
  // How much a non-path link matters: 2 both ends highlighted, 1 one end, 0 background.
  const linkTier = useCallback((l: L) => {
    const g = graphRef.current;
    const a = endId(l.source) as string;
    const b = endId(l.target) as string;
    return (isKey(roleOf(a, g)) ? 1 : 0) + (isKey(roleOf(b, g)) ? 1 : 0);
  }, []);
  const linkColor = useCallback((l: L) => {
    const C = colors();
    if (linkOnPath(l)) return C.path;
    if (mapRef.current) return alpha(C.suspect, 0.6);
    const g = graphRef.current;
    const a = endId(l.source) as string;
    const b = endId(l.target) as string;
    if (g.symptomatic.includes(a) && g.symptomatic.includes(b)) return alpha(C.symptomatic, 0.55);
    const t = linkTier(l);
    if (t === 2) return alpha(C.edge, 0.5);
    return alpha(C.edge, faded(a) || faded(b) ? 0.06 : t ? 0.2 : 0.12);
  }, [linkOnPath, linkTier, faded]);
  const linkWidth = useCallback((l: L) => (linkOnPath(l) ? 3.5 : mapRef.current ? 1.2 : linkTier(l) === 2 ? 1.4 : 0.8), [linkOnPath, linkTier]);
  // Arrowheads only where they carry meaning: many faint arrows into the alert read as a brush.
  const linkArrow = useCallback((l: L) => (linkOnPath(l) ? 6 : linkTier(l) === 2 || mapRef.current ? 4 : 0), [linkOnPath, linkTier]);
  const linkVisible = useCallback((l: L) => {
    const a = endId(l.source) as string;
    const b = endId(l.target) as string;
    if (hidden(a) || hidden(b)) return false;
    if (!mapRef.current) return true;
    // The map draws only the dependencies of the selected or hovered service.
    const on = (id: string) => graphRef.current.suspects.includes(id) || hoverRef.current === id;
    return on(a) || on(b);
  }, [hidden]);
  // New function identity only when the path changes, which makes the library re-seed particles.
  const particles = useMemo(() => {
    const keys = pathKeys;
    return (l: L) => (keys.has(`${endId(l.source)}>${endId(l.target)}`) ? 3 : 0);
  }, [pathKeys]);

  const hovered = hover ? { role: roleOf(hover.id, graph), clause: graph.eliminated[hover.id] } : null;

  return (
    <div ref={box} className={`relative h-full w-full overflow-hidden transition-opacity duration-200 ${fitted ? "opacity-100" : "opacity-0"}`} role="img" aria-label={isMap ? "Service map, grouped by region and domain. The list beside it has every service." : "Service graph. A text list of the causal path is beside the canvas."}>
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
        linkDirectionalArrowLength={linkArrow as never}
        linkDirectionalArrowRelPos={0.92}
        linkDirectionalParticles={particles as never}
        linkDirectionalParticleWidth={4}
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
          <div className="text-dim">{isMap ? (graph.suspects.includes(hover.id) ? "Selected target" : "Click to select") : ROLE_LABEL[hovered.role]}</div>
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
