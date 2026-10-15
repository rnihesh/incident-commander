// Deterministic fixture graph for the hero scenario. Layered left to right:
// edges run cause -> consumer (the IMPACTS direction), so the alerting service
// sits near the right and its upstream closure fans out to the left.
// Layout is computed here from a seeded RNG so it is identical on every load
// (KTD21: precomputed layout, no force simulation at runtime).

export type NodeRole = "alerting" | "symptomatic" | "suspect" | "eliminated" | "path" | "plain";

export interface GraphNode {
  id: string;
  name: string;
  tier: number;
  x: number;
  y: number;
}
export interface GraphEdge {
  source: string;
  target: string;
}

const REGION = "us-east";
const sid = (n: string) => `svc-${n}@${REGION}`;

const TIERS: string[][] = [
  ["config-store", "feature-flags", "secrets", "dns-ctl", "mesh-ctl", "kafka-bus", "redis-pool", "pg-orders", "pg-catalog", "cdn-edge"],
  ["tax", "fx-rates", "inventory", "catalog", "promo", "pricing", "loyalty", "geo", "identity", "fraud-score", "ratelimit", "search"],
  ["tax-calc", "stock-reserve", "product-api", "discount", "wallet", "quote-engine", "address-verify", "session", "risk-gate", "recs", "shipping-rates", "currency"],
  ["shipping", "payments-gw", "basket-merge", "cart", "gift-cards", "subscription", "bnpl", "webhook-in", "audit", "coupon-eval"],
  ["order-api", "storefront-bff", "checkout", "mobile-api"],
  ["fulfilment", "notify", "email", "invoice", "analytics-ingest", "admin-bff", "returns", "reporting", "search-bff", "ab-assign", "crm-sync", "loyalty-points"],
];

// Nodes that must sit on the central band so the causal path reads as a line.
const CENTER = new Set(["pricing", "quote-engine", "cart", "checkout", "order-api"]);

function mulberry32(seed: number) {
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

function build() {
  const rnd = mulberry32(0x1c0de);
  const W = 1180;
  const H = 720;
  const nodes: GraphNode[] = [];
  TIERS.forEach((names, tier) => {
    const list = [...names];
    const ci = list.findIndex((n) => CENTER.has(n));
    if (ci >= 0) {
      const mid = Math.floor(list.length / 2);
      [list[ci], list[mid]] = [list[mid], list[ci]];
    }
    list.forEach((n, i) => {
      const x = 60 + (tier * (W - 120)) / (TIERS.length - 1) + (rnd() - 0.5) * 34;
      const slot = (H - 100) / list.length;
      const base = 50 + slot * (i + 0.5);
      const isCenter = CENTER.has(n);
      const y = isCenter ? H / 2 : base + (rnd() - 0.5) * slot * 0.45;
      nodes.push({ id: sid(n), name: n, tier, x: Math.round(x), y: Math.round(y) });
    });
  });

  const edges: GraphEdge[] = [];
  const seen = new Set<string>();
  const add = (s: string, t: string) => {
    const k = `${s}>${t}`;
    if (s !== t && !seen.has(k)) {
      seen.add(k);
      edges.push({ source: s, target: t });
    }
  };
  const byTier = (t: number) => nodes.filter((n) => n.tier === t);
  for (let t = 0; t < TIERS.length - 1; t++) {
    const here = byTier(t);
    const next = byTier(t + 1);
    here.forEach((n, i) => {
      const center = Math.round((i / Math.max(here.length - 1, 1)) * (next.length - 1));
      const picks = 1 + Math.floor(rnd() * 2);
      for (let k = 0; k < picks; k++) {
        const j = Math.min(next.length - 1, Math.max(0, center + Math.round((rnd() - 0.5) * 4)));
        add(n.id, next[j].id);
      }
    });
    // keep every next-tier node reachable
    next.forEach((m, j) => {
      if (!edges.some((e) => e.target === m.id)) {
        const i = Math.round((j / Math.max(next.length - 1, 1)) * (here.length - 1));
        add(here[i].id, m.id);
      }
    });
  }
  // The hero causal path and neighbours.
  const forced: [string, string][] = [
    ["pricing", "quote-engine"],
    ["quote-engine", "cart"],
    ["cart", "checkout"],
    ["checkout", "order-api"],
    ["payments-gw", "checkout"],
    ["shipping", "checkout"],
    ["tax", "tax-calc"],
    ["tax-calc", "cart"],
    ["checkout", "storefront-bff"],
    ["checkout", "mobile-api"],
    ["feature-flags", "discount"],
    ["discount", "coupon-eval"],
    ["coupon-eval", "cart"],
  ];
  forced.forEach(([a, b]) => add(sid(a), sid(b)));
  return { nodes, edges };
}

const built = build();
export const HERO_NODES: GraphNode[] = built.nodes;
export const HERO_EDGES: GraphEdge[] = built.edges;

export const HERO_ALERTING = sid("checkout");
export const HERO_PATH: string[] = ["pricing", "quote-engine", "cart", "checkout"].map(sid);
export const HERO_SYMPTOMATIC: string[] = [
  "quote-engine",
  "cart",
  "checkout",
  "order-api",
  "storefront-bff",
  "mobile-api",
  "payments-gw",
].map(sid);
export const HERO_SUSPECT_SERVICES: string[] = ["pricing", "feature-flags", "tax"].map(sid);
export const HERO_LATE: string[] = [sid("webhook-in")];
/** Node that only appears when a re-run is FALSIFIED (demo of the "new node" legend). */
export const HERO_FALSIFIER_NODE = sid("coupon-eval");

/** Final roles for the hero run, for fixtures/tests; the console derives roles from events. */
export const HERO_ROLES: Record<string, NodeRole> = (() => {
  const r: Record<string, NodeRole> = {};
  HERO_NODES.forEach((n) => (r[n.id] = "plain"));
  HERO_SYMPTOMATIC.forEach((id) => (r[id] = "symptomatic"));
  HERO_SUSPECT_SERVICES.forEach((id) => (r[id] = "suspect"));
  HERO_PATH.forEach((id) => (r[id] = "path"));
  r[HERO_ALERTING] = "alerting";
  return r;
})();

export const HERO_SERVICE_IDS: string[] = HERO_NODES.map((n) => n.id);
export const nodeIds = (names: string[]) => names.map(sid);
export { sid as serviceId };
