"use client";

import { RESULT_VIEW, hashCheck, type Comparison, type RevealResponse } from "../lib/judge";
import { Badge, Button, CopyButton, Glyph, PanelHead } from "./ui";

function Row({ k, v, mono = true }: { k: string; v: React.ReactNode; mono?: boolean }) {
  return (
    <div className="grid grid-cols-[7.5rem_minmax(0,1fr)] gap-2 py-0.5 text-label">
      <dt className="text-dim">{k}</dt>
      <dd className={`${mono ? "font-mono" : ""} break-all text-ink`}>{v}</dd>
    </div>
  );
}

function VerdictSide({ title, c }: { title: string; c: Comparison }) {
  const view = RESULT_VIEW[c.result];
  return (
    <div className="min-w-0 border border-rule bg-void/40 px-3 py-2" data-testid={`verdict-${title}`}>
      <h3 className="mb-1 text-label font-semibold text-ink">{title}</h3>
      <dl>
        <Row k="outcome" v={c.outcome ?? "no verdict"} />
        <Row k="root named" v={c.suspect_id ?? "none"} />
        <Row k="implied class" v={c.claimed_class ?? "none"} />
        {c.rival_ids.length > 0 && <Row k="rivals" v={c.rival_ids.join(", ")} />}
      </dl>
      <div className="mt-2 flex items-center gap-2">
        <Badge tone={view.tone} size="lg">{view.label}</Badge>
      </div>
      <p className="mt-1 text-meta text-dim">{c.explanation}</p>
    </div>
  );
}

/**
 * The commitment, prominent before and during the run; after the verdict (or the 120 s
 * timeout) a Reveal button opens it: committed fault beside the agent's verdict, graded
 * MATCH / PARTIAL / NO CLAIM, and the hash recomputed from the revealed values.
 */
export function CommitReveal({
  commitment,
  incidentId,
  canReveal,
  secondsLeft,
  reveal,
  browserHash,
  onReveal,
  revealing,
  error,
}: {
  commitment: string;
  incidentId: string;
  canReveal: boolean;
  secondsLeft: number;
  reveal: RevealResponse | null;
  browserHash: string | null;
  onReveal: () => void;
  revealing: boolean;
  error: string | null;
}) {
  const check = reveal ? hashCheck(commitment, reveal, browserHash) : null;
  return (
    <section aria-labelledby="commit-h" className="border-b border-rule">
      <PanelHead
        id="commit-h"
        title="Commitment"
        meta={incidentId}
        right={
          !reveal && (
            <Button
              variant="primary"
              onClick={onReveal}
              disabled={!canReveal || revealing}
              title={canReveal ? undefined : "Opens at the verdict or 120 s after injection"}
            >
              {revealing ? "Revealing" : canReveal ? "Reveal" : `Reveal in ${secondsLeft}s`}
            </Button>
          )
        }
      />
      <div className="px-inset py-3">
        <p className="text-meta text-dim">sha256(canonical fault JSON + nonce), published before the agent started</p>
        <p className="mt-1 break-all font-mono text-body leading-snug text-ink" data-testid="commitment">
          {commitment}
        </p>
        <div className="mt-1.5 flex items-center gap-2">
          <CopyButton text={commitment} label="Copy hash" />
          {!reveal && <span className="text-meta text-faint">The fault class, root and nonce stay on the server until the reveal.</span>}
        </div>
        {error && <p role="alert" className="mt-2 text-label text-falsified">{error}</p>}
      </div>

      {reveal && check && (
        <div className="arrive border-t border-rule px-inset py-3" data-testid="reveal">
          <div className="grid grid-cols-2 gap-gutter">
            <div className="min-w-0 border border-rule bg-void/40 px-3 py-2">
              <h3 className="mb-1 text-label font-semibold text-ink">Committed fault</h3>
              <dl>
                <Row k="fault class" v={reveal.revealed.fault_class} />
                <Row k="root" v={reveal.revealed.root_id} />
                <Row k="fault id" v={reveal.revealed.fault_id} />
                <Row k="nonce" v={reveal.revealed.nonce} />
              </dl>
            </div>
            <VerdictSide title="Agent verdict (blind)" c={reveal.comparison} />
          </div>
          {reveal.timed_out && (
            <p className="mt-2 text-meta text-drifted">Revealed at the 120 s timeout, before the run reached a verdict.</p>
          )}
          {reveal.rederived && (
            <div className="mt-2">
              <VerdictSide title="Re-derived verdict" c={reveal.rederived} />
            </div>
          )}
          <div className="mt-3 border-l-2 border-rule-hi bg-void/60 px-3 py-2" data-testid="hash-check">
            <p className="font-mono text-meta text-dim">sha256({reveal.preimage} + nonce)</p>
            <p className="mt-1 flex items-center gap-2 font-mono text-label">
              <span className={check.ok ? "text-verified" : "text-falsified"}><Glyph tone={check.ok ? "verified" : "falsified"} /></span>
              <span className="break-all text-ink">{reveal.recomputed}</span>
            </p>
            <p className={`mt-1 text-label ${check.ok ? "text-verified" : "text-falsified"}`}>
              {check.ok ? "Recomputed hash = committed hash" : "Recomputed hash differs from the committed hash"}
              {check.browser === true && " (also recomputed in this browser)"}
              {check.browser === false && " (this browser computed a different hash)"}
            </p>
          </div>
        </div>
      )}
    </section>
  );
}
