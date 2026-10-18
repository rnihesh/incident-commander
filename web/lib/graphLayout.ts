// Client-side layouts for the graph canvas. The seeded world positions come from one spring
// layout of every service, so an incident's closure or the whole map drawn at those positions is
// a hairball. These lay the same nodes out by structure instead.

export interface Pt {
  x: number;
  y: number;
}

interface Edge {
  source: string;
  target: string;
}

/** Spacing for the layered layout, in canvas units (about screen pixels at zoom 1). */
export const LAYER = { colW: 190, gap: 16, mid: 30, big: 46 } as const;

/**
 * Layered left-to-right layout of an incident closure. Edges run in the IMPACTS direction
 * (cause -> consumer), so the alerting service sits in the right-hand column and each column to
 * its left is one more hop upstream: a causal path origin -> hops -> alerting reads as a line from
 * left to right. Symptomatic services the alert does not reach share the alert's column.
 *
 * Within a column, nodes sit at the mean height of their neighbours in the adjacent columns, then
 * are packed apart in that order (one pass towards the alert, then refined), so a chain of
 * single dependencies stays a straight horizontal line. `weight` gives a node more room: 2 for
 * the alerting and symptomatic services, 1 for services with recent changes (future suspects),
 * 0 for the rest.
 */
export function layeredLayout(ids: string[], edges: Edge[], alert: string | null, weight: (id: string) => 0 | 1 | 2): Map<string, Pt> {
  const known = new Set(ids);
  const up = new Map<string, string[]>(); // node -> services that impact it
  const down = new Map<string, string[]>(); // node -> services it impacts
  for (const e of edges) {
    if (!known.has(e.source) || !known.has(e.target) || e.source === e.target) continue;
    (up.get(e.target) ?? up.set(e.target, []).get(e.target)!).push(e.source);
    (down.get(e.source) ?? down.set(e.source, []).get(e.source)!).push(e.target);
  }

  // Hop distance upstream of the alert; unreached symptomatic services start their own walk at 0.
  const dist = new Map<string, number>();
  const walk = (seeds: string[]) => {
    const q = seeds.filter((s) => !dist.has(s));
    q.forEach((s) => dist.set(s, 0));
    for (let i = 0; i < q.length; i++)
      for (const u of up.get(q[i]) ?? [])
        if (!dist.has(u)) {
          dist.set(u, dist.get(q[i])! + 1);
          q.push(u);
        }
  };
  if (alert && known.has(alert)) walk([alert]);
  walk(ids.filter((id) => weight(id) === 2 && !dist.has(id)).sort());
  const deepest = Math.max(0, ...dist.values());
  for (const id of ids) if (!dist.has(id)) dist.set(id, deepest + 1);

  const cols: string[][] = [];
  for (const id of [...ids].sort()) (cols[dist.get(id)!] ??= []).push(id);
  const y = new Map<string, number>();
  const room = (id: string) => [LAYER.gap, LAYER.mid, LAYER.big][weight(id)];

  // Column 0: the alert in the middle, the other symptomatic services alternating around it.
  const head = (cols[0] ?? []).sort((a, b) => (a === alert ? -1 : b === alert ? 1 : weight(b) - weight(a) || a.localeCompare(b)));
  const slot = new Map(head.map((id, i) => [id, i === 0 ? 0 : (i % 2 ? -1 : 1) * Math.ceil(i / 2) * LAYER.big]));
  head.sort((a, b) => slot.get(a)! - slot.get(b)!);
  pack(head, head.map((id) => slot.get(id)!), room, y);

  const mean = (xs: string[]) => {
    const vs = xs.filter((n) => y.has(n)).map((n) => y.get(n)!);
    return vs.length ? vs.reduce((a, b) => a + b, 0) / vs.length : null;
  };
  const place = (d: number, both: boolean) => {
    const col = cols[d] ?? [];
    const want = new Map<string, number>();
    for (const id of col) {
      const dn = mean((down.get(id) ?? []).filter((n) => dist.get(n)! < d));
      const un = both ? mean((up.get(id) ?? []).filter((n) => dist.get(n)! > d)) : null;
      // Downstream (towards the alert) counts double: it is the direction the path reads.
      const w = dn !== null && un !== null ? (2 * dn + un) / 3 : (dn ?? un ?? mean([...(down.get(id) ?? []), ...(up.get(id) ?? [])]) ?? 0);
      want.set(id, w);
    }
    // Heavier nodes first among equals so the important ones keep the centre line.
    col.sort((a, b) => want.get(a)! - want.get(b)! || weight(b) - weight(a) || a.localeCompare(b));
    pack(col, col.map((id) => want.get(id)!), room, y);
  };
  for (let d = 1; d < cols.length; d++) place(d, false);
  for (let it = 0; it < 2; it++) for (let d = 1; d < cols.length; d++) place(d, true);

  const out = new Map<string, Pt>();
  for (const id of ids) out.set(id, { x: dist.get(id)! ? -dist.get(id)! * LAYER.colW : 0, y: y.get(id) ?? 0 });
  return out;
}

/** Places `order` (already sorted) as close to `want` as possible (least squares), keeping each
 *  neighbour pair at least the mean of their `room` apart. With `off` the cumulative minimum
 *  offsets, node i sits at block.pos + off[i]; adjacent blocks merge while out of order. */
export function pack(order: string[], want: number[], room: (id: string) => number, out: Map<string, number>) {
  type Block = { first: number; last: number; pos: number; sum: number; n: number };
  const off: number[] = [];
  order.forEach((id, i) => (off[i] = i ? off[i - 1] + (room(order[i - 1]) + room(id)) / 2 : 0));
  const blocks: Block[] = [];
  order.forEach((_, i) => {
    let b: Block = { first: i, last: i, sum: want[i] - off[i], n: 1, pos: want[i] - off[i] };
    while (blocks.length && blocks[blocks.length - 1].pos > b.pos) {
      const prev = blocks.pop()!;
      const sum = prev.sum + b.sum;
      const n = prev.n + b.n;
      b = { first: prev.first, last: b.last, sum, n, pos: sum / n };
    }
    blocks.push(b);
  });
  for (const b of blocks) for (let i = b.first; i <= b.last; i++) out.set(order[i], b.pos + off[i]);
}

/** A caption drawn on the canvas at a fixed screen size (region and domain names on the map). */
export interface Caption {
  x: number;
  y: number;
  text: string;
  kind: "region" | "domain";
}

interface MapService {
  id: string;
  region: string;
  domain: string;
  tier: number;
  name: string;
}

const REGION_ORDER = ["us-east", "eu-west", "ap-south"];
/** Domains in a fixed order so each one sits in the same slot in every region. */
const DOMAIN_ORDER = ["edge", "commerce", "payments", "identity", "growth", "logistics", "data", "platform"];
const SEED_GAP = 9;

/**
 * The judge's target map: one column per region, and inside it one disc per business domain
 * (the same slot in every region), services packed in a sunflower from the core tier outwards.
 * Returns positions plus the region and domain captions.
 */
export function clusterLayout(services: MapService[]): { pos: Map<string, Pt>; captions: Caption[] } {
  const rank = (xs: string[], order: string[]) =>
    [...new Set(xs)].sort((a, b) => (order.indexOf(a) + 1 || 99) - (order.indexOf(b) + 1 || 99) || a.localeCompare(b));
  const regions = rank(services.map((s) => s.region), REGION_ORDER);
  const domains = rank(services.map((s) => s.domain), DOMAIN_ORDER);
  const groups = new Map<string, MapService[]>();
  for (const s of services) {
    const k = `${s.region}|${s.domain}`;
    (groups.get(k) ?? groups.set(k, []).get(k)!).push(s);
  }
  const biggest = Math.max(1, ...[...groups.values()].map((g) => g.length));
  const discR = SEED_GAP * Math.sqrt(biggest) * 0.62 + 6;
  const cellW = discR * 2 + 22;
  const cellH = discR * 2 + 34; // room for the domain caption above each disc
  const perRow = 2;
  const regionW = cellW * perRow + 50;

  const pos = new Map<string, Pt>();
  const captions: Caption[] = [];
  regions.forEach((r, ri) => {
    const x0 = ri * regionW;
    captions.push({ x: x0 + (cellW * perRow) / 2, y: -30, text: r, kind: "region" });
    domains.forEach((d, di) => {
      const g = groups.get(`${r}|${d}`);
      if (!g?.length) return;
      const cx = x0 + (di % perRow) * cellW + cellW / 2;
      const cy = Math.floor(di / perRow) * cellH + cellH / 2 + 8;
      g.sort((a, b) => a.tier - b.tier || a.name.localeCompare(b.name));
      // Sunflower (golden-angle) packing: even density, round outline, deterministic.
      g.forEach((s, i) => {
        const rr = SEED_GAP * 0.62 * Math.sqrt(i + 0.5);
        const th = i * 2.399963;
        pos.set(s.id, { x: cx + rr * Math.cos(th), y: cy + rr * Math.sin(th) });
      });
      const outer = SEED_GAP * 0.62 * Math.sqrt(g.length) + 8;
      captions.push({ x: cx, y: cy - outer - 10, text: d, kind: "domain" });
    });
  });
  return { pos, captions };
}
