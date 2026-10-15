"use client";

import { useState, type ButtonHTMLAttributes, type HTMLAttributes, type InputHTMLAttributes, type ReactNode, type SelectHTMLAttributes } from "react";
import type { XrayResponse } from "../lib/api";
import { XRAY_NOTE, fmtMs, naiveHighlight, xrayRows } from "../lib/xray";

export type Tone = "verified" | "drifted" | "falsified" | "tampered" | "degraded" | "info" | "muted";

export const TONE: Record<Tone, { text: string; border: string; bg: string }> = {
  verified: { text: "text-verified", border: "border-verified/60", bg: "bg-verified/10" },
  drifted: { text: "text-drifted", border: "border-drifted/60", bg: "bg-drifted/10" },
  falsified: { text: "text-falsified", border: "border-falsified/60", bg: "bg-falsified/10" },
  tampered: { text: "text-tampered", border: "border-tampered/60", bg: "bg-tampered/10" },
  degraded: { text: "text-degraded", border: "border-degraded/60", bg: "bg-degraded/10 stripe" },
  info: { text: "text-symptom", border: "border-symptom/50", bg: "bg-symptom/10" },
  muted: { text: "text-dim", border: "border-rule-hi", bg: "bg-raise" },
};

export function Glyph({ tone, size = 12 }: { tone: Tone; size?: number }) {
  const common = { width: size, height: size, viewBox: "0 0 12 12", fill: "none", stroke: "currentColor", strokeWidth: 1.8, "aria-hidden": true } as const;
  switch (tone) {
    case "verified":
      return <svg {...common}><path d="M2 6.5l2.6 2.6L10 3.4" /></svg>;
    case "drifted":
      return <svg {...common}><path d="M6 1.8L11 10H1z" strokeLinejoin="round" /></svg>;
    case "falsified":
      return <svg {...common}><path d="M2.5 2.5l7 7M9.5 2.5l-7 7" /></svg>;
    case "tampered":
      return <svg {...common}><path d="M1.5 6h3M7.5 6h3M5 3.2l-.6 5.6M7 3.2l.6 5.6" strokeLinecap="round" /></svg>;
    case "degraded":
      return <svg {...common}><rect x="1.8" y="1.8" width="8.4" height="8.4" /><path d="M2 8l6-6M4 10l6-6" strokeWidth="1" /></svg>;
    case "info":
      return <svg {...common}><circle cx="6" cy="6" r="3.6" /></svg>;
    default:
      return <svg {...common}><path d="M2 6h8" /></svg>;
  }
}

/** Verdict/state badge. Always carries a text label and an icon, never colour alone.
 *  A tinted fill with no outline, so a row of badges reads as state rather than as buttons. */
export function Badge({ tone, children, title, size = "sm" }: { tone: Tone; children: ReactNode; title?: string; size?: "sm" | "lg" }) {
  const t = TONE[tone];
  return (
    <span
      title={title}
      className={`inline-flex items-center gap-1.5 rounded-ctl font-sans font-semibold leading-none tracking-[0.02em] whitespace-nowrap ${t.text} ${t.bg} ${
        size === "lg" ? "px-2.5 py-1.5 text-label" : "px-1.5 py-[3px] text-meta"
      }`}
    >
      <Glyph tone={tone} size={size === "lg" ? 12 : 10} />
      {children}
    </span>
  );
}

export type ButtonVariant = "primary" | "secondary" | "ghost" | "danger";
export type ButtonSize = "sm" | "md";

const BTN_BASE =
  "inline-flex items-center justify-center gap-1.5 rounded-ctl border font-sans font-medium whitespace-nowrap transition-colors disabled:cursor-not-allowed disabled:opacity-40";
const BTN_VARIANT: Record<ButtonVariant, string> = {
  // One accent per page: the primary action is the only filled control.
  primary: "border-ink bg-ink text-void hover:border-white hover:bg-white",
  secondary: "border-rule-hi bg-raise text-ink hover:border-dim",
  ghost: "border-transparent text-dim hover:bg-raise hover:text-ink",
  danger: "border-falsified/50 text-falsified hover:bg-falsified/10",
};
const BTN_SIZE: Record<ButtonSize, string> = {
  sm: "h-6 px-2 text-meta",
  md: "h-7 px-2.5 text-label",
};

/** Class string for anything that should look like a button (links, summary elements). */
export function buttonClass(variant: ButtonVariant = "secondary", size: ButtonSize = "md", extra = "") {
  return `${BTN_BASE} ${BTN_VARIANT[variant]} ${BTN_SIZE[size]} ${extra}`.trim();
}

export function Button({
  variant = "secondary",
  size = "md",
  className = "",
  type = "button",
  ...rest
}: { variant?: ButtonVariant; size?: ButtonSize } & ButtonHTMLAttributes<HTMLButtonElement>) {
  return <button type={type} className={buttonClass(variant, size, className)} {...rest} />;
}

/** A small pressable or static tag: clause filters, counts, segmented options. */
export function Chip({
  active = false,
  dim = false,
  onClick,
  children,
  title,
  mono = true,
}: {
  active?: boolean;
  dim?: boolean;
  onClick?: () => void;
  children: ReactNode;
  title?: string;
  mono?: boolean;
}) {
  const cls = `inline-flex h-6 items-center gap-1 rounded-ctl border px-2 text-meta whitespace-nowrap ${mono ? "font-mono" : "font-sans"} ${
    active ? "border-symptom/70 bg-symptom/15 text-ink" : "border-rule-hi text-dim"
  } ${dim && !active ? "opacity-40" : ""}`;
  if (!onClick) return <span className={cls} title={title}>{children}</span>;
  return (
    <button type="button" aria-pressed={active} onClick={onClick} title={title} className={`${cls} ${active ? "" : "hover:border-dim hover:text-ink"}`}>
      {children}
    </button>
  );
}

const FIELD_CTL = "h-7 rounded-ctl border border-rule-hi bg-void px-2 text-label text-ink hover:border-dim disabled:cursor-not-allowed disabled:opacity-40";

/** Native select in the console's field style. `mono` for values that are ids (fault classes, change ids). */
export function Select({ mono = true, className = "", ...rest }: { mono?: boolean } & SelectHTMLAttributes<HTMLSelectElement>) {
  return <select className={`${FIELD_CTL} ${mono ? "font-mono" : "font-sans"} ${className}`} {...rest} />;
}

/** Native text/number/datetime input in the same field style. */
export function Input({ mono = true, className = "", ...rest }: { mono?: boolean } & InputHTMLAttributes<HTMLInputElement>) {
  return <input className={`${FIELD_CTL} ${mono ? "font-mono" : "font-sans"} ${className}`} {...rest} />;
}

/** A labelled control. `inline` puts the label beside the control (toolbars); otherwise above it (forms). */
export function Field({ label, htmlFor, inline = false, hint, children, className = "" }: { label: ReactNode; htmlFor?: string; inline?: boolean; hint?: ReactNode; children: ReactNode; className?: string }) {
  return (
    <div className={`${inline ? "flex items-center gap-2" : "flex flex-col gap-1"} ${className}`}>
      <label htmlFor={htmlFor} className="text-meta text-dim">{label}</label>
      {children}
      {hint && <p className="text-meta text-faint">{hint}</p>}
    </div>
  );
}

/** Segmented single choice (radio group) such as as-known / as-true. */
export function Segmented<T extends string>({ name, legend, value, options, onChange }: { name: string; legend: string; value: T; options: { value: T; label: ReactNode }[]; onChange: (v: T) => void }) {
  return (
    <fieldset className="flex h-7 items-stretch overflow-hidden rounded-ctl border border-rule-hi">
      <legend className="sr-only">{legend}</legend>
      {options.map((o) => (
        <label
          key={o.value}
          className={`flex cursor-pointer items-center px-2.5 font-mono text-meta has-[:focus-visible]:outline-2 has-[:focus-visible]:outline-symptom ${
            value === o.value ? "bg-symptom/15 text-ink" : "text-dim hover:text-ink"
          }`}
        >
          <input type="radio" name={name} value={o.value} checked={value === o.value} onChange={() => onChange(o.value)} className="sr-only" />
          {o.label}
        </label>
      ))}
    </fieldset>
  );
}

/** The one container for a block of content: square, ruled, on the deck surface. */
export function Panel({
  children,
  className = "",
  as: Tag = "section",
  ...rest
}: { children: ReactNode; className?: string; as?: "section" | "div" | "aside" } & HTMLAttributes<HTMLElement>) {
  return <Tag className={`border border-rule bg-deck ${className}`} {...rest}>{children}</Tag>;
}

/** Hash with the leading 8 bytes bright and the tail dimmed; full value on copy. */
export function Hash({ value, head = 10, full = false }: { value: string | null; head?: number; full?: boolean }) {
  if (!value) return <span className="font-mono text-faint">genesis</span>;
  return (
    <span className="font-mono tabular-nums" title={value}>
      <span className="text-ink">{value.slice(0, head)}</span>
      <span className="hash-tail">{full ? value.slice(head) : `${value.slice(head, head + 6)}`}</span>
    </span>
  );
}

export function CopyButton({ text, label = "Copy" }: { text: string; label?: string }) {
  const [done, setDone] = useState(false);
  return (
    <button
      type="button"
      onClick={async () => {
        try {
          await navigator.clipboard.writeText(text);
          setDone(true);
          setTimeout(() => setDone(false), 1400);
        } catch {
          /* clipboard unavailable (insecure context): leave label unchanged */
        }
      }}
      className={buttonClass("secondary", "sm")}
      aria-live="polite"
    >
      {done ? "Copied" : label}
    </button>
  );
}

/** A stored query. With `xray` (console surfaces with a live incident only), an "X-ray"
 *  toggle fetches FalkorDB's plan and live profile of this exact query on the snapshot. */
export function Cypher({ text, params, xray }: { text: string; params?: unknown; xray?: () => Promise<XrayResponse> }) {
  const [open, setOpen] = useState(false);
  const [plan, setPlan] = useState<{ data?: XrayResponse; error?: string; loading?: boolean }>({});
  const toggle = async () => {
    const next = !open;
    setOpen(next);
    if (!next || !xray || plan.data || plan.loading) return;
    setPlan({ loading: true });
    try {
      setPlan({ data: await xray() });
    } catch (e) {
      setPlan({ error: e instanceof Error ? e.message : "X-ray failed" });
    }
  };
  return (
    <div className="border-l-2 border-rule-hi bg-void/70">
      {xray && (
        <div className="flex justify-end border-b border-rule px-2 py-1">
          <button
            type="button"
            aria-expanded={open}
            onClick={toggle}
            title="FalkorDB's execution plan and a live profile of this query on the incident snapshot"
            className={buttonClass("secondary", "sm")}
          >
            {open ? "Hide X-ray" : "X-ray"}
          </button>
        </div>
      )}
      <pre className="whitespace-pre-wrap px-3 py-2 font-mono text-label leading-[1.55] text-ink/90 [overflow-wrap:anywhere]">
        {highlight(text)}
      </pre>
      {params !== undefined && (
        <pre className="whitespace-pre-wrap border-t border-rule px-3 py-1.5 font-mono text-meta text-dim [overflow-wrap:anywhere]">
          params {JSON.stringify(params)}
        </pre>
      )}
      {xray && open && (
        <div className="border-t border-rule" aria-live="polite">
          {plan.loading && <p className="px-3 py-2 text-label text-faint">Asking FalkorDB for the plan...</p>}
          {plan.error && <p className="px-3 py-2 text-label text-falsified" role="alert">{plan.error}</p>}
          {plan.data && <XrayPlan data={plan.data} />}
        </div>
      )}
    </div>
  );
}

/** The operator rows of one plan. `hit` flags the naive plan's Filter and Traverse rows. */
function PlanTable({ rows, hit }: { rows: ReturnType<typeof xrayRows>; hit?: { filter: number | null; traverse: number } | null }) {
  const stats = rows.some((r) => r.records !== null || r.ms !== null);
  return (
    <div className="overflow-x-auto">
      <table className="w-full font-mono text-meta">
        <thead>
          <tr className="text-left text-faint">
            <th className="py-0.5 font-normal">operator</th>
            {stats && <th className="py-0.5 text-right font-normal">records</th>}
            {stats && <th className="py-0.5 text-right font-normal">time</th>}
          </tr>
        </thead>
        <tbody>
          {rows.map((r, i) => {
            const mark = hit ? (hit.filter === i ? "filter" : hit.traverse === i ? "traverse" : null) : null;
            return (
              <tr
                key={i}
                data-naive-hit={mark ?? undefined}
                className={`border-t border-rule/60 align-top ${mark ? "border-l-2 border-l-drifted bg-drifted/10" : ""}`}
              >
                <td className="py-0.5 pr-3" style={{ paddingLeft: `${r.depth * 14}px` }}>
                  <span className={mark || /Variable Length Traverse|^Filter$/.test(r.name) ? "text-drifted" : "text-ink"}>{r.name}</span>
                  {mark === "filter" && <span className="ml-2 font-sans text-faint">runs after the traversal below</span>}
                  {r.args && <span className="block break-all text-dim">{r.args}</span>}
                </td>
                {stats && <td className="py-0.5 text-right tabular-nums text-ink">{r.records ?? ""}</td>}
                {stats && <td className="py-0.5 pl-3 text-right tabular-nums whitespace-nowrap text-dim">{fmtMs(r.ms)}</td>}
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

/** FalkorDB's operator tree (GRAPH.EXPLAIN) with per-operator records and time from a live
 *  GRAPH.PROFILE. When the server also sends the naive plan (valid time filtered inside the
 *  traversal) the two sit side by side when the panel itself is wide (a container query, so a
 *  narrow funnel column stacks them), then the note on why the product builds as-of snapshots. */
export function XrayPlan({ data }: { data: XrayResponse }) {
  const rows = xrayRows(data.explain, data.profile);
  const total = rows.reduce((t, r) => t + (r.ms ?? 0), 0);
  const naive = data.naive;
  const run = (
    <div className="min-w-0">
      {naive && <p className="mb-1 text-label font-semibold text-ink">What we run: on the as-of snapshot</p>}
      <div className="mb-1 flex flex-wrap items-baseline justify-between gap-2 text-meta text-dim">
        <span>Execution plan, live profile</span>
        <span className="font-mono text-faint">{data.graph} · {fmtMs(total)} total</span>
      </div>
      <PlanTable rows={rows} />
    </div>
  );
  return (
    <div className="@container px-3 py-2">
      {naive ? (
        <div className="grid gap-4 @min-[720px]:grid-cols-2">
          <div className="min-w-0">
            <p className="mb-1 text-label font-semibold text-drifted">What we avoid: valid-time filter inside a variable-length traversal</p>
            <Cypher text={naive.cypher} />
            <div className="mb-1 mt-2 flex flex-wrap items-baseline justify-between gap-2 text-meta text-dim">
              <span>Execution plan (EXPLAIN)</span>
              <span className="font-mono text-faint">{naive.graph_label}</span>
            </div>
            <PlanTable rows={xrayRows(naive.explain, naive.explain)} hit={naiveHighlight(naive.explain)} />
          </div>
          {run}
        </div>
      ) : (
        run
      )}
      <p className="mt-2 text-label leading-[1.5] text-dim">{naive && "The left plan shows it. "}{XRAY_NOTE}</p>
    </div>
  );
}

const KW = /\b(MATCH|WHERE|RETURN|UNWIND|WITH|CALL|YIELD|ORDER BY|LIMIT|AS|IN|AND|OR|CASE|WHEN|THEN|END|DISTINCT|collect|count|size|all|length|nodes)\b/g;
function highlight(src: string): ReactNode[] {
  const out: ReactNode[] = [];
  src.split("\n").forEach((line, li) => {
    if (li) out.push("\n");
    if (line.trimStart().startsWith("//")) {
      out.push(<span key={`c${li}`} className="text-faint">{line}</span>);
      return;
    }
    let last = 0;
    let m: RegExpExecArray | null;
    KW.lastIndex = 0;
    while ((m = KW.exec(line))) {
      if (m.index > last) out.push(line.slice(last, m.index));
      const algo = /^(CALL)$/.test(m[1]);
      out.push(<span key={`k${li}-${m.index}`} className={algo ? "text-symptom" : "text-drifted/90"}>{m[1]}</span>);
      last = m.index + m[1].length;
    }
    out.push(line.slice(last));
  });
  return out;
}

export function PanelHead({ title, meta, right, id }: { title: string; meta?: ReactNode; right?: ReactNode; id?: string }) {
  return (
    <div className="flex min-h-10 items-center justify-between gap-3 border-b border-rule px-inset py-2">
      <h2 id={id} className="flex min-w-0 flex-wrap items-baseline gap-x-2.5 text-body font-semibold text-ink sm:flex-nowrap">
        <span className="shrink-0">{title}</span>
        {meta && <span className="min-w-0 text-meta font-normal text-dim sm:truncate">{meta}</span>}
      </h2>
      {right && <div className="flex shrink-0 items-center gap-2">{right}</div>}
    </div>
  );
}

export function Empty({ children }: { children: ReactNode }) {
  return <p className="max-w-[72ch] px-inset py-4 text-body text-faint">{children}</p>;
}

/** Loading or empty placeholder that fills its box and centres one line of copy. */
export function Placeholder({ children, className = "" }: { children: ReactNode; className?: string }) {
  return <div className={`grid h-full min-h-24 place-items-center px-8 text-center text-body text-faint ${className}`}>{children}</div>;
}

export function shortSvc(id: string) {
  return id.replace(/^svc-/, "").replace(/@.*$/, "");
}

/** Short names, keeping the region only where two ids share a name (multi-region services). */
export function shortSvcs(ids: string[]): string[] {
  const counts = new Map<string, number>();
  for (const id of ids) counts.set(shortSvc(id), (counts.get(shortSvc(id)) ?? 0) + 1);
  return ids.map((id) => {
    const name = shortSvc(id);
    const region = id.includes("@") ? id.slice(id.indexOf("@") + 1) : "";
    return (counts.get(name) ?? 0) > 1 && region ? `${name}@${region}` : name;
  });
}
