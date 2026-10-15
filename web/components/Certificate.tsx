"use client";

import { useMemo, useState } from "react";
import { CLAUSES, type CertificateData, type Clause } from "../lib/payloads";
import { Badge, Chip, Empty, Hash, PanelHead, shortSvc } from "./ui";

const hhmm = (iso: string) => iso.slice(11, 16) + "Z";

export type SegState = "passed" | "failed" | "unreached";

/** One state per clause in CLAUSES order: passed before the failing clause, failed at it, unreached after. A null failing clause (the root) passes all five. */
export function clauseSegments(failing: Clause | null): SegState[] {
  const at = failing ? CLAUSES.indexOf(failing) : CLAUSES.length;
  return CLAUSES.map((_, i) => (i < at ? "passed" : i === at ? "failed" : "unreached"));
}

const SEG_CLASS: Record<SegState, string> = {
  passed: "bg-verified/70",
  failed: "bg-drifted",
  unreached: "bg-rule",
};

export function ClauseBar({ failing }: { failing: Clause | null }) {
  const segs = clauseSegments(failing);
  return (
    <span
      className="inline-flex gap-px align-middle"
      role="img"
      aria-label={failing ? `fails ${failing}` : "passes every clause"}
      data-testid="clause-bar"
    >
      {segs.map((st, i) => (
        <span key={CLAUSES[i]} data-seg={CLAUSES[i]} data-state={st} title={`${CLAUSES[i]}: ${st === "unreached" ? "not reached" : st}`} className={`h-2 w-3 ${SEG_CLASS[st]}`} />
      ))}
    </span>
  );
}

export interface StripMark {
  id: string;
  kind: "onset" | "suspect" | "change";
  t: number;
  after: boolean; // change landed after onset
}

export function stripMarks(cert: CertificateData, rows: { change: string; event_time?: string }[]): StripMark[] {
  if (!cert.onset) return [];
  const onset = Date.parse(cert.onset);
  const marks: StripMark[] = [{ id: "onset", kind: "onset", t: onset, after: false }];
  if (cert.suspect_event_time) {
    const t = Date.parse(cert.suspect_event_time);
    marks.push({ id: cert.suspect, kind: "suspect", t, after: t >= onset });
  }
  for (const r of rows) {
    if (!r.event_time) continue;
    const t = Date.parse(r.event_time);
    marks.push({ id: r.change, kind: "change", t, after: t >= onset });
  }
  return marks;
}

function TimeStrip({ marks }: { marks: StripMark[] }) {
  const lo = Math.min(...marks.map((m) => m.t));
  const hi = Math.max(...marks.map((m) => m.t));
  const span = Math.max(hi - lo, 1);
  const onset = marks[0];
  const late = marks.filter((m) => m.kind !== "onset" && m.after).length;
  return (
    <div className="mt-3" data-testid="time-strip">
      <div className="relative h-5 border-y border-rule bg-deck" role="img" aria-label={`Change times against onset ${hhmm(new Date(onset.t).toISOString())}`}>
        {marks.map((m) => (
          <span
            key={m.kind + m.id}
            data-mark={m.kind}
            data-after={m.after ? "true" : "false"}
            title={m.kind === "onset" ? `onset ${hhmm(new Date(m.t).toISOString())}` : `${m.id} ${hhmm(new Date(m.t).toISOString())}${m.after ? " (after onset)" : ""}`}
            style={{ left: `${((m.t - lo) / span) * 100}%` }}
            className={`absolute top-0 h-full -translate-x-1/2 ${
              m.kind === "onset" ? "w-0.5 bg-symptom" : m.kind === "suspect" ? "w-1 bg-verified" : m.after ? "w-px bg-late" : "w-px bg-dim"
            }`}
          />
        ))}
      </div>
      <p className="mt-1 text-meta text-faint">
        <span className="text-symptom">onset {hhmm(new Date(onset.t).toISOString())}</span>
        {late > 0 && <span className="ml-2 text-late">{late} change{late > 1 ? "s" : ""} after onset</span>}
      </p>
    </div>
  );
}

export function Certificate({
  cert,
  view,
  onFocus,
}: {
  cert: CertificateData | null;
  view: "true" | "known";
  onFocus: (ids: string[]) => void;
}) {
  const [clause, setClause] = useState<Clause | "all">("all");
  const rows = useMemo(() => {
    if (!cert) return [];
    const visible = cert.checked.filter((c) => view === "true" || !c.late);
    // Near misses first: the deeper the clause, the closer the change came to qualifying.
    return [...visible].sort((a, b) => CLAUSES.indexOf(b.clause) - CLAUSES.indexOf(a.clause));
  }, [cert, view]);
  const counts = useMemo(() => {
    const c: Record<Clause, number> = { after_onset: 0, origin: 0, coverage: 0, order: 0, signature: 0 };
    rows.forEach((r) => c[r.clause]++);
    return c;
  }, [rows]);
  const shown = clause === "all" ? rows : rows.filter((r) => r.clause === clause);
  const hiddenLate = cert ? cert.checked.length - rows.length : 0;

  return (
    <section aria-labelledby="cert-h">
      <PanelHead
        id="cert-h"
        title="Absence certificate"
        right={cert && (cert.qualifying === 0 ? <Badge tone="verified">0 QUALIFYING</Badge> : <Badge tone="drifted">{cert.qualifying} RIVAL QUALIFIES</Badge>)}
      />
      {!cert ? (
        <Empty>Issued after the suspect is chosen: every other change in the window, with the first clause it fails.</Empty>
      ) : (
        <div className="arrive px-inset py-3">
          <p className="text-body leading-snug text-ink/90">
            Checked <b className="font-mono font-medium text-ink">{rows.length}</b> other changes across{" "}
            <b className="font-mono font-medium text-ink">{cert.services_checked}</b> services between{" "}
            <span className="font-mono">{hhmm(cert.window.from)}</span> and <span className="font-mono">{hhmm(cert.window.to)}</span>.{" "}
            {cert.qualifying === 0 ? (
              <>None qualifies besides <span className="font-mono text-verified">{cert.suspect}</span>.</>
            ) : (
              <>{cert.qualifying} rival still qualifies, so no single cause is claimed.</>
            )}
          </p>
          {hiddenLate > 0 && (
            <p className="mt-1 text-label text-late">
              As known at alert time: {hiddenLate} late-arriving record{hiddenLate > 1 ? "s" : ""} excluded. Switch to as-true to include.
            </p>
          )}
          <div className="mt-3 flex flex-wrap gap-1.5" role="group" aria-label="Filter disqualified changes by failing clause">
            <Chip active={clause === "all"} onClick={() => setClause("all")}>all {rows.length}</Chip>
            {CLAUSES.map((c) => (
              <Chip key={c} active={clause === c} dim={!counts[c]} onClick={() => setClause(c)}>
                {c} {counts[c]}
              </Chip>
            ))}
          </div>
          {(() => {
            const marks = stripMarks(cert, rows);
            return marks.length > 1 ? <TimeStrip marks={marks} /> : null;
          })()}
          <div className="mt-3 max-h-48 overflow-y-auto border border-rule" tabIndex={0} aria-label="Disqualified changes">
            <table className="w-full border-collapse font-mono text-meta">
              <thead className="sticky top-0 bg-raise text-left font-sans text-faint">
                <tr><th className="px-2 py-1 font-normal">change</th><th className="px-2 py-1 font-normal">service</th><th className="px-2 py-1 font-normal">fails</th><th className="px-2 py-1 font-normal">clauses</th></tr>
              </thead>
              <tbody>
                {clause === "all" && (
                  <tr className="border-t border-rule/60">
                    <td className={`px-2 py-[3px] ${cert.suspect_clause ? "text-ink" : "text-verified"}`}>{cert.suspect}</td>
                    <td className="px-2 py-[3px] text-dim">suspect</td>
                    <td className={`px-2 py-[3px] ${cert.suspect_clause ? "text-drifted" : "text-verified"}`}>{cert.suspect_clause ?? "passes all"}</td>
                    <td className="px-2 py-[3px]"><ClauseBar failing={cert.suspect_clause ?? null} /></td>
                  </tr>
                )}
                {shown.slice(0, 120).map((r) => (
                  <tr
                    key={r.change}
                    className="cursor-pointer border-t border-rule/60 hover:bg-raise"
                    onClick={() => onFocus([r.service])}
                  >
                    <td className="px-2 py-[3px] text-ink">{r.change}{r.late && <span className="ml-1.5 text-late">late</span>}</td>
                    <td className="px-2 py-[3px] text-dim">{shortSvc(r.service)}</td>
                    <td className="px-2 py-[3px] text-drifted">{r.clause}</td>
                    <td className="px-2 py-[3px]"><ClauseBar failing={r.clause} /></td>
                  </tr>
                ))}
              </tbody>
            </table>
            {shown.length > 120 && <p className="border-t border-rule px-2 py-1 text-meta text-faint">Showing 120 of {shown.length}. Filter by clause to narrow.</p>}
          </div>
          <p className="mt-2 flex items-center gap-2 text-meta text-faint">certificate <Hash value={cert.hash} head={16} /></p>
        </div>
      )}
    </section>
  );
}
