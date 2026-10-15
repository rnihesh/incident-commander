"use client";

import { useEffect, useState } from "react";
import { getLearningCurve, type LearningCurveResponse } from "../lib/api";
import { placePointLabels, type Box } from "../lib/chartLabels";
import { PanelHead, shortSvc } from "./ui";

const W = 520;
const H = 150;
const PAD = { top: 20, right: 8, bottom: 32, left: 8 };
const RECALL_THRESHOLD = 0.25; // api ic.tools.recall.MIN_CONFIDENCE (runs carry their own)

const conf2 = (c: number) => c.toFixed(2);
/** 10px JetBrains Mono: advance per character and a cap-height box, in viewBox units. */
const CH_W = 6.1;
const LABEL_H = 9;

/** Bar per incident: registry tool calls in one session with memory. First and second
 *  incidents are highlighted (the F3 comparison); later ones are muted. Runs that record
 *  the learned edge's confidence (forgetting curve, R9) also get a 0..1 confidence line over
 *  the bars, contradicting incidents marked on it, and the recall threshold. */
export function LearningCurveChart({ data }: { data: LearningCurveResponse }) {
  const rows = data.incidents.filter((r) => typeof r.tool_calls === "number");
  if (!rows.length) return <p className="px-inset py-4 text-label text-faint">The learning-curve run recorded no incidents.</p>;
  const max = Math.max(...rows.map((r) => r.tool_calls as number), 1);
  const slot = (W - PAD.left - PAD.right) / rows.length;
  const bw = Math.min(56, slot * 0.6);
  const ih = H - PAD.top - PAD.bottom;
  const first = rows[0].tool_calls as number;
  const second = rows.length > 1 ? (rows[1].tool_calls as number) : null;
  const pct = second !== null && first ? Math.round(100 * (1 - second / first)) : null;
  const threshold = data.meta.forget?.threshold ?? RECALL_THRESHOLD;
  const confs = rows.map((r) => (typeof r.edge_confidence === "number" ? r.edge_confidence : null));
  const hasConf = confs.some((c) => c !== null);
  const cx = (i: number) => PAD.left + slot * i + slot / 2;
  const cy = (c: number) => H - PAD.bottom - Math.max(0, Math.min(1, c)) * ih;
  const pts = confs.flatMap((c, i) => (c === null ? [] : [`${cx(i).toFixed(1)},${cy(c).toFixed(1)}`]));
  const contradictions = rows.filter((r) => r.contradicted).length;
  const peakAt = confs.reduce<number>((best, c, i) => (c !== null && (best < 0 || c > (confs[best] as number)) ? i : best), -1);
  const last = [...confs].reverse().find((c) => c !== null) ?? null;
  const fellAt = contradictions ? rows.findIndex((r, i) => r.contradicted && (confs[i] ?? 1) < threshold) : -1;
  const bars = rows.map((r, i) => {
    const v = r.tool_calls as number;
    const h = (v / max) * ih * (hasConf ? 0.78 : 1); // headroom for the 0..1 line
    const x = PAD.left + slot * i + (slot - bw) / 2;
    const y = H - PAD.bottom - h;
    // With the confidence line drawn over the bars, values sit inside tall-enough bars so the
    // line's markers and labels never land on them.
    const inside = hasConf && h >= 16;
    return { v, h, x, y, inside, ty: inside ? y + 12 : y - 4 };
  });
  // Confidence labels (the peak and each contradiction) offset to a side that clears the line,
  // its markers, the recall threshold and the bar values.
  const confPts = confs.flatMap((c, i) => (c === null ? [] : [{ i, x: cx(i), y: cy(c), r: rows[i].contradicted ? 3.5 : 2.5 }]));
  const labelled = confPts.filter((p) => rows[p.i].contradicted || p.i === peakAt);
  const confText = (i: number) => `${conf2(confs[i] as number)}${rows[i].contradicted ? " ÷2" : ""}`;
  const barBoxes: Box[] = bars.map((b) => {
    const w = String(b.v).length * 6.6 + 2; // 11px digits plus a little air
    return { x0: b.x + bw / 2 - w / 2, y0: b.ty - LABEL_H - 1, x1: b.x + bw / 2 + w / 2, y1: b.ty + 1 };
  });
  const placed = placePointLabels(
    labelled.map((p) => ({ x: p.x, y: p.y, r: p.r + 0.75, w: confText(p.i).length * CH_W, h: LABEL_H })),
    confPts,
    confPts,
    { x0: 0, y0: 0, x1: W, y1: H - PAD.bottom - 1 },
    [...barBoxes, { x0: PAD.left, y0: cy(threshold) - 0.5, x1: W - PAD.right, y1: cy(threshold) + 0.5 }],
  );
  const labelAt = new Map(labelled.map((p, k) => [p.i, placed[k]]));
  const label =
    `Tool calls per incident: ${rows.map((r, i) => `incident ${i + 1} ${r.tool_calls}`).join(", ")}` +
    (hasConf ? `. Learned-edge confidence per incident: ${confs.map((c) => (c === null ? "n/a" : conf2(c))).join(", ")}` : "");
  return (
    <figure className="px-inset py-3">
      <figcaption className="mb-2 max-w-[72ch] text-label text-ink/90">
        {second !== null ? (
          <>
            Incident 1 took <span className="font-mono text-ink">{first}</span> tool calls; the second, downstream of the same learned coupling,
            took <span className="font-mono text-verified">{second}</span>
            {pct !== null && pct > 0 && <> ({pct}% fewer)</>}.
          </>
        ) : (
          <>Incident 1 took {first} tool calls.</>
        )}
        {hasConf && peakAt >= 0 && contradictions > 0 && (
          <>
            {" "}
            Edge confidence peaked at <span className="font-mono text-verified">{conf2(confs[peakAt] as number)}</span>, then {contradictions} contradicting
            incident{contradictions === 1 ? "" : "s"} halved it to <span className="font-mono text-falsified">{conf2(last as number)}</span>
            {fellAt >= 0 && <> (under the recall threshold from incident {fellAt + 1})</>}.
          </>
        )}
      </figcaption>
      <svg viewBox={`0 0 ${W} ${H}`} className="w-full" role="img" aria-label={label}>
        <title>{label}</title>
        <line x1={PAD.left} x2={W - PAD.right} y1={H - PAD.bottom} y2={H - PAD.bottom} stroke="var(--color-rule-hi)" />
        {rows.map((r, i) => {
          const { v, h, x, y, inside, ty } = bars[i];
          const fill = i === 0 ? "var(--color-drifted)" : i === 1 ? "var(--color-verified)" : "var(--color-rule-hi)";
          return (
            <g key={`${r.incident_id}-${i}`} data-bar={i + 1} data-value={v}>
              <rect x={x} y={y} width={bw} height={h} fill={fill} opacity={i < 2 ? 0.9 : 0.75} />
              <text x={x + bw / 2} y={ty} textAnchor="middle" className={`${inside && i < 2 ? "fill-void" : "fill-ink"} font-mono`} fontSize="11">
                {v}
              </text>
              <text x={x + bw / 2} y={H - PAD.bottom + 14} textAnchor="middle" className="fill-dim font-mono" fontSize="10">
                #{i + 1}
              </text>
              <text x={x + bw / 2} y={H - PAD.bottom + 26} textAnchor="middle" className="fill-faint font-sans" fontSize="10">
                {r.memory_seeded ? "memory" : "funnel"}
              </text>
            </g>
          );
        })}
        {hasConf && (
          <g data-confidence-overlay="">
            <line
              data-threshold={threshold}
              x1={PAD.left}
              x2={W - PAD.right}
              y1={cy(threshold)}
              y2={cy(threshold)}
              stroke="var(--color-falsified)"
              strokeDasharray="3 3"
              opacity={0.7}
            />
            <polyline data-confidence-line="" points={pts.join(" ")} fill="none" stroke="var(--color-symptom)" strokeWidth={1.5} />
            {confs.map((c, i) => {
              if (c === null) return null;
              const bad = !!rows[i].contradicted;
              // Label only the points the caption talks about: the peak and each contradiction.
              const at = labelAt.get(i);
              return (
                <g key={`c-${rows[i].incident_id}-${i}`} data-confidence={c} data-contradicted={bad ? "true" : undefined}>
                  <circle
                    cx={cx(i)}
                    cy={cy(c)}
                    r={bad ? 3.5 : 2.5}
                    fill={bad ? "var(--color-void)" : "var(--color-symptom)"}
                    stroke={bad ? "var(--color-falsified)" : "var(--color-symptom)"}
                    strokeWidth={1.5}
                  />
                  {at && (
                    <text x={at.x} y={at.y} textAnchor={at.anchor} className={`${bad ? "fill-falsified" : "fill-symptom"} font-mono`} fontSize="10" paintOrder="stroke" stroke="var(--color-deck)" strokeWidth={3}>
                      {confText(i)}
                    </text>
                  )}
                </g>
              );
            })}
          </g>
        )}
      </svg>
      {hasConf && (
        <p className="mt-1.5 flex flex-wrap items-center gap-x-4 gap-y-1 text-meta text-dim" aria-hidden>
          <span className="flex items-center gap-1.5"><i className="inline-block h-px w-4 bg-symptom" />edge confidence</span>
          <span className="flex items-center gap-1.5"><i className="inline-block w-4 border-t border-dashed border-falsified/70" />recall threshold {threshold}</span>
          <span className="flex items-center gap-1.5"><i className="inline-block size-2 rounded-full border border-falsified" />contradicted</span>
        </p>
      )}
      <p className="mt-1 text-meta text-faint">
        {data.meta.coupling ? (
          <>
            Learned <span className="font-mono">{shortSvc(data.meta.coupling.caller)} → {shortSvc(data.meta.coupling.callee)}</span>.{" "}
          </>
        ) : null}
        Memory only reorders the work: every verdict still carries all its proofs.
      </p>
    </figure>
  );
}

/** Console side panel: the published learning-curve run (`ic learn`, GET /api/learning-curve). */
export function LearningCurve() {
  const [data, setData] = useState<LearningCurveResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    let cancelled = false;
    getLearningCurve()
      .then((d) => !cancelled && setData(d))
      .catch((e) => !cancelled && setError(e instanceof Error ? e.message : "unavailable"));
    return () => {
      cancelled = true;
    };
  }, []);
  return (
    <section aria-labelledby="lc-h">
      <PanelHead id="lc-h" title="Learning curve" meta={data ? `${data.incidents.length} incidents, one session` : undefined} />
      {data ? (
        <LearningCurveChart data={data} />
      ) : (
        <p className="px-inset py-4 text-label text-faint">
          {error ? "No learning-curve run published yet (run `ic learn`)." : "Loading the learning-curve run."}
        </p>
      )}
    </section>
  );
}
