"use client";

import { useState } from "react";
import type { ProofEntry } from "@/lib/reducer";
import type { VerifyResult } from "@/lib/mock/verify";
import { Badge, Button, CopyButton, Cypher, Empty, Hash, PanelHead } from "./ui";
import type { ChainVerify } from "@/lib/verify";
import { VerifyBadge, VerifyButton } from "./VerifyButton";
import { xrayStep } from "@/lib/api";
import { proofSeq } from "@/lib/xray";

export function HypothesisChain({
  proofs,
  results,
  running,
  onVerify,
  onVerifyAll,
  onFocusNode,
  anchored,
  chain,
  incidentId = null,
}: {
  proofs: ProofEntry[];
  results: Record<string, VerifyResult>;
  running: Record<string, boolean>;
  onVerify: (id: string) => void;
  onVerifyAll: () => void;
  onFocusNode: (node: string) => void;
  anchored: boolean;
  chain?: ChainVerify | null;
  /** The live incident (enables X-ray on each proof's query); null for recordings. */
  incidentId?: string | null;
}) {
  const [open, setOpen] = useState<Record<string, boolean>>({});
  const head = proofs[proofs.length - 1];
  const anyRunning = Object.values(running).some(Boolean);
  const headResult = head ? results[head.hypothesis_id] : undefined;
  return (
    <section aria-labelledby="chain-h" className="min-h-0">
      <PanelHead
        id="chain-h"
        title="Hypothesis chain"
        meta={proofs.length ? `${proofs.length} proof${proofs.length > 1 ? "s" : ""}, append-only` : undefined}
        right={
          <VerifyButton
            label="Verify chain"
            variant="secondary"
            disabled={!proofs.length}
            running={anyRunning}
            onClick={onVerifyAll}
          />
        }
      />
      {!proofs.length ? (
        <Empty>Each hypothesis is registered, executed, and hashed into this chain. Proofs appear as the agent tests suspects.</Empty>
      ) : (
        <ol className="relative px-inset py-3">
          {proofs.map((p, i) => {
            const res = results[p.hypothesis_id];
            const isOpen = open[p.hypothesis_id];
            const seq = proofSeq(p.hypothesis_id);
            return (
              <li key={p.hypothesis_id} className="arrive relative grid grid-cols-[22px_1fr] gap-x-3 pb-4 last:pb-0">
                <div className="relative flex justify-center">
                  {i < proofs.length - 1 && <span className="absolute top-4 bottom-[-16px] w-px bg-rule-hi" aria-hidden />}
                  <span
                    className={`z-[1] mt-1 grid size-4 place-items-center rounded-[2px] border font-mono text-meta leading-none ${
                      p.state === "HOLDS" ? "border-verified bg-verified/20 text-verified" : "border-falsified bg-falsified/15 text-falsified"
                    }`}
                    aria-hidden
                  >
                    {p.position}
                  </span>
                </div>
                <div className="min-w-0">
                  <div className="flex flex-wrap items-center gap-x-2.5 gap-y-1">
                    <span className="font-mono text-label text-ink">{p.hypothesis?.suspect ?? p.hypothesis_id}</span>
                    <Badge tone={p.state === "HOLDS" ? "verified" : "falsified"} title="Hypothesis outcome">
                      {p.state}
                    </Badge>
                    <span className="font-mono text-meta text-faint">proof {p.position} of chain</span>
                  </div>
                  <p className="mt-1 text-body text-ink/90">{p.hypothesis?.statement ?? p.summary}</p>
                  {p.hypothesis && <p className="text-label text-dim">{p.summary}</p>}
                  <dl className="mt-1.5 grid grid-cols-[auto_1fr] gap-x-3 gap-y-0.5 text-meta">
                    <dt className="text-faint">prev</dt>
                    <dd><Hash value={p.prev_hash} head={16} /></dd>
                    <dt className="text-faint">hash</dt>
                    <dd className="flex items-center gap-2">
                      <Hash value={p.hash} head={16} />
                      <CopyButton text={p.hash} />
                    </dd>
                    {res && res.recomputed !== p.hash && (
                      <>
                        <dt className="text-faint">re-run</dt>
                        <dd><Hash value={res.recomputed} head={16} /></dd>
                      </>
                    )}
                  </dl>
                  <div className="mt-2 flex flex-wrap items-center gap-2">
                    <Button
                      size="sm"
                      aria-expanded={!!isOpen}
                      onClick={() => setOpen((o) => ({ ...o, [p.hypothesis_id]: !o[p.hypothesis_id] }))}
                    >
                      {isOpen ? "Hide query" : "Show query"}
                    </Button>
                    <VerifyButton
                      label="Re-run"
                      running={running[p.hypothesis_id]}
                      result={res}
                      onClick={() => onVerify(p.hypothesis_id)}
                      onFocusNode={onFocusNode}
                    />
                  </div>
                  {res && res.state !== "VERIFIED" && <p className="mt-1 text-label text-dim">{res.detail}</p>}
                  {isOpen && (
                    <div className="mt-2">
                      <div className="mb-1 flex justify-end"><CopyButton text={p.query} label="Copy query" /></div>
                      <Cypher
                        text={p.query}
                        params={p.params}
                        xray={incidentId && seq !== null && p.query ? () => xrayStep(incidentId, { proof: seq }) : undefined}
                      />
                    </div>
                  )}
                </div>
              </li>
            );
          })}
        </ol>
      )}
      {head && (
        <div className="flex flex-wrap items-center gap-x-3 gap-y-1 border-t border-rule px-inset py-2 text-meta">
          <span className="text-faint">chain head</span>
          <Hash value={head.hash} head={24} />
          <span className="text-faint">{anchored ? "anchored at completion" : "not yet anchored"}</span>
          {headResult?.state === "TAMPERED" && !chain && <Badge tone="tampered">TAMPERED</Badge>}
          {chain && <VerifyBadge result={chain.result} />}
          {chain && (
            <span className="font-mono text-faint">
              chain {chain.chain_ok ? "ok" : "broken"} · anchor {chain.anchor_ok ? "ok" : "not confirmed"}
            </span>
          )}
        </div>
      )}
      {chain?.result.reasons && chain.result.state === "TAMPERED" && (
        <ul className="border-t border-rule px-inset py-2 text-label text-tampered" role="alert">
          {chain.result.reasons.map((r) => <li key={r}>{r}</li>)}
        </ul>
      )}
    </section>
  );
}
