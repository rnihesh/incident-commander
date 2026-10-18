"use client";

import { useCallback, useEffect, useState, type ReactNode } from "react";
import { plainText } from "../lib/plain";
import {
  FallbackError,
  NetworkError,
  getProof,
  isProofRef,
  reverifyProof,
  type ProofPageProof,
  type ProofPageResponse,
  type ProofVerifyResponse,
} from "../lib/api";
import type { VerifyState } from "../lib/payloads";
import { Badge, Button, CopyButton, Cypher, Hash, PanelHead, type Tone } from "./ui";
import { PageHeader, PageNav } from "./Shell";
import { VerifyButton } from "./VerifyButton";

const TONE_FOR: Record<VerifyState, Tone> = {
  VERIFIED: "verified",
  DRIFTED: "drifted",
  UNANCHORED: "info",
  FALSIFIED: "falsified",
  TAMPERED: "tampered",
};

const DETAIL: Record<VerifyState, string> = {
  VERIFIED: "Every proof re-ran against the PR's world copy to the same result; chain and anchor intact.",
  DRIFTED: "A proof's result changed but every conclusion still holds.",
  UNANCHORED: "The chain is intact but was never anchored, so nothing outside the graph vouches for it.",
  FALSIFIED: "A recorded conclusion no longer holds on the PR's world copy.",
  TAMPERED: "A stored proof, the chain links or the anchor do not match.",
};

const KIND_LABEL: Record<string, string> = {
  suspect_qualifies: "suspect qualifies",
  suspect_necessary: "suspect necessary",
  no_other_qualifying: "absence certificate",
  entity_refuted: "rival refuted",
};

const asList = <T,>(v: T[] | T | undefined | null): T[] => (v == null ? [] : Array.isArray(v) ? v : [v]);
const isState = (s: string): s is VerifyState => s in TONE_FOR;

/** Every query of one proof, in order, as one copyable block. */
export function proofQueryText(p: ProofPageProof): string {
  const cy = asList(p.cypher);
  const params = asList(p.params);
  return cy.map((q, i) => `${q}\n// params ${JSON.stringify(params[i] ?? {})}`).join("\n\n");
}

function StateBadge({ state, size }: { state: string; size?: "sm" | "lg" }) {
  return isState(state) ? (
    <Badge tone={TONE_FOR[state]} size={size} title={DETAIL[state]}>
      {state}
    </Badge>
  ) : (
    <Badge tone="muted" size={size}>{state}</Badge>
  );
}

function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="min-w-0">
      <dt className="text-meta text-faint">{label}</dt>
      <dd className="mt-0.5 text-body text-ink [overflow-wrap:anywhere]">{children}</dd>
    </div>
  );
}

function ProofItem({ p, last, check }: { p: ProofPageProof; last: boolean; check?: ProofVerifyResponse["proofs"][number] }) {
  const [open, setOpen] = useState(false);
  const queries = asList(p.cypher);
  const params = asList(p.params);
  return (
    <li className="relative grid grid-cols-[22px_1fr] gap-x-3 pb-5 last:pb-0" data-seq={p.seq}>
      <div className="relative flex justify-center">
        {!last && <span className="absolute top-4 bottom-[-20px] w-px bg-rule-hi" aria-hidden />}
        <span
          className={`z-[1] mt-1 grid size-4 place-items-center rounded-[2px] border font-mono text-meta leading-none ${
            p.state === "HOLDS" ? "border-verified bg-verified/20 text-verified" : "border-falsified bg-falsified/15 text-falsified"
          }`}
          aria-hidden
        >
          {p.seq}
        </span>
      </div>
      <div className="min-w-0">
        <div className="flex flex-wrap items-center gap-x-2.5 gap-y-1">
          <span className="font-mono text-label text-ink">{p.entity_id}</span>
          <Badge tone={p.state === "HOLDS" ? "verified" : "falsified"} title="Recorded outcome">
            {p.state}
          </Badge>
          <span className="font-mono text-meta text-faint">{KIND_LABEL[p.kind] ?? p.kind}</span>
          {check && <StateBadge state={check.status} />}
        </div>
        <p className="mt-1 text-body text-ink/90">{plainText(p.claim)}</p>
        <dl className="mt-1.5 grid grid-cols-[auto_1fr] gap-x-3 gap-y-0.5 text-meta">
          <dt className="text-faint">prev</dt>
          <dd><Hash value={p.prev_hash} head={16} /></dd>
          <dt className="text-faint">hash</dt>
          <dd className="flex items-center gap-2">
            <Hash value={p.hash} head={16} />
            <CopyButton text={p.hash} />
          </dd>
        </dl>
        {check && check.status !== "VERIFIED" && (
          <p className="mt-1 text-label text-dim">
            {check.tampered_reason ??
              (check.responsible.length ? `Responsible: ${check.responsible.slice(0, 6).join(", ")}` : `Re-run status ${check.status}.`)}
            {check.late_arriving && <span className="ml-1.5 text-late">late-arriving: {check.late_ids.join(", ")}</span>}
          </p>
        )}
        {queries.length > 0 && (
          <div className="mt-2 flex flex-wrap items-center gap-2">
            <Button size="sm" aria-expanded={open} onClick={() => setOpen((o) => !o)}>
              {open ? "Hide query" : queries.length > 1 ? `Show queries (${queries.length})` : "Show query"}
            </Button>
            <CopyButton text={proofQueryText(p)} label={queries.length > 1 ? "Copy queries" : "Copy query"} />
          </div>
        )}
        {open && (
          <div className="mt-2 space-y-2">
            {queries.map((q, i) => (
              <Cypher key={i} text={q} params={params[i]} />
            ))}
          </div>
        )}
      </div>
    </li>
  );
}

/** Read-only proof page body; ``verify`` is the latest re-verify (or the cached one). */
export function ProofView({
  data,
  verify,
  running,
  verifyError,
  onVerify,
}: {
  data: ProofPageResponse;
  verify: ProofVerifyResponse | null;
  running: boolean;
  verifyError: string | null;
  onVerify: () => void;
}) {
  const checks = new Map((verify?.proofs ?? []).map((c) => [c.seq, c]));
  const status = verify?.status;
  return (
    <div className="mx-auto max-w-[1100px] space-y-5 px-4 py-6 sm:px-8">
      <section aria-labelledby="proof-h" className="border border-rule bg-deck">
        <div className="flex flex-wrap items-start justify-between gap-4 border-b border-rule px-inset py-4">
          <div className="min-w-0">
            <p className="text-label text-dim"><span className="font-mono">{data.incident_id}</span> · public proof</p>
            <h1 id="proof-h" className="mt-1 text-display font-semibold tracking-tight text-ink">{data.title}</h1>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <Badge tone={data.verdict === "DIAGNOSED" ? "verified" : "drifted"} size="lg">{data.verdict}</Badge>
            {data.anchored ? (
              <Badge tone={data.anchor_matches ? "verified" : "tampered"} size="lg" title="Chain head anchored in the API's store at completion">
                {data.anchor_matches ? "ANCHORED" : "ANCHOR MISMATCH"}
              </Badge>
            ) : (
              <Badge tone="info" size="lg">UNANCHORED</Badge>
            )}
          </div>
        </div>
        <dl className="grid grid-cols-2 gap-x-6 gap-y-3 px-inset py-4 lg:grid-cols-4">
          <Field label="Alert service"><span className="font-mono">{data.alert_service ?? "n/a"}</span></Field>
          <Field label="Alert time"><span className="font-mono">{data.alert_time ?? "n/a"}</span></Field>
          <Field label="Root cause"><span className="font-mono">{data.suspect ?? "not proven"}</span></Field>
          <Field label="Rehearsal">
            {data.rehearsal ? (
              <span className="font-mono" title={data.rehearsal.detail ?? undefined}>
                {data.rehearsal.result}
                {data.rehearsal.checked != null && <span className="text-dim"> · {data.rehearsal.checked} re-checked</span>}
              </span>
            ) : (
              <span className="text-dim">none recorded</span>
            )}
          </Field>
        </dl>
        <div className="flex flex-wrap items-center gap-x-4 gap-y-2 border-t border-rule px-inset py-3 text-label">
          <span className="text-faint">chain head</span>
          <Hash value={data.chain_head} head={24} />
          {data.chain_head && <CopyButton text={data.chain_head} />}
          {data.pr.url && (
            <a href={data.pr.url} className="font-mono text-symptom underline decoration-dotted underline-offset-2 hover:text-ink" rel="noreferrer noopener" target="_blank">
              PR {data.pr.number != null ? `#${data.pr.number}` : "link"}
              {data.pr.simulated ? " (simulated)" : ""}
            </a>
          )}
          {data.pr.expires_at && <span className="text-faint">auto-closes {data.pr.expires_at.slice(11, 16)}Z</span>}
        </div>
      </section>

      <section aria-labelledby="reverify-h" className="border border-rule bg-deck">
        <PanelHead
          id="reverify-h"
          title="Re-verify"
          meta={`re-runs every proof against this PR's world copy · ${data.status_context}`}
          right={<VerifyButton label="Re-verify" variant="primary" running={running} disabled={!data.proofs.length} onClick={onVerify} />}
        />
        <div className="px-inset py-3 text-body" aria-live="polite">
          {verifyError ? (
            <p className="text-drifted">{verifyError}</p>
          ) : !verify ? (
            <p className="text-faint">Not re-verified yet. Re-verify replays each recorded falsifier on fresh as-of snapshots of the PR&apos;s world copy.</p>
          ) : (
            <div className="space-y-1.5">
              <div className="flex flex-wrap items-center gap-3">
                {status && <StateBadge state={status} size="lg" />}
                <span className="font-mono text-label text-dim">
                  chain {verify.chain_ok ? "ok" : `broken${verify.broken_at != null ? ` at ${verify.broken_at}` : ""}`} · anchor{" "}
                  {verify.anchor_ok == null ? "absent" : verify.anchor_ok ? "ok" : "mismatch"} · {verify.proofs.length} proofs · {Math.round(verify.ms)} ms
                </span>
                {verify.cached && <span className="font-mono text-meta text-faint">re-verified moments ago (cached)</span>}
              </div>
              {status && isState(status) && <p className="text-dim">{DETAIL[status]}</p>}
              {verify.commit_status && (
                <p className="font-mono text-meta text-faint">
                  commit status {verify.commit_status.posted ? verify.commit_status.state : "not posted"}
                  {verify.commit_status.simulated ? " (simulated)" : ""}
                  {verify.commit_status.reason ? ` · ${verify.commit_status.reason}` : ""}
                </p>
              )}
              {verify.tampered_reasons.length > 0 && (
                <ul className="text-label text-tampered" role="alert">
                  {verify.tampered_reasons.map((r) => <li key={r}>{r}</li>)}
                </ul>
              )}
            </div>
          )}
        </div>
      </section>

      <section aria-labelledby="chain-h" className="border border-rule bg-deck">
        <PanelHead id="chain-h" title="Hypothesis chain" meta={`${data.proofs.length} proof${data.proofs.length === 1 ? "" : "s"}, append-only`} />
        {data.chain_error && <p className="px-inset py-2 text-label text-tampered" role="alert">{data.chain_error}</p>}
        {data.proofs.length ? (
          <ol className="px-inset py-4">
            {data.proofs.map((p, i) => (
              <ProofItem key={p.proof_id} p={p} last={i === data.proofs.length - 1} check={checks.get(p.seq)} />
            ))}
          </ol>
        ) : (
          <p className="px-inset py-5 text-body text-faint">No proofs on this chain.</p>
        )}
      </section>
    </div>
  );
}

export default function ProofPage({ proofRef }: { proofRef: string }) {
  const valid = isProofRef(proofRef);
  const [data, setData] = useState<ProofPageResponse | null>(null);
  const [loadError, setLoadError] = useState<string | null>(valid ? null : "This proof link is not valid.");
  const [verify, setVerify] = useState<ProofVerifyResponse | null>(null);
  const [running, setRunning] = useState(false);
  const [verifyError, setVerifyError] = useState<string | null>(null);

  useEffect(() => {
    if (!valid) return;
    let cancelled = false;
    getProof(proofRef)
      .then((d) => {
        if (cancelled) return;
        setData(d);
        setVerify(d.last_verify ? { ...d.last_verify, cached: true } : null);
      })
      .catch((e) => {
        if (cancelled) return;
        setLoadError(
          e instanceof NetworkError && e.status === 404
            ? "Proof not found. Rollback PRs auto-close after an hour and their proofs go with them."
            : "The proof service is unavailable right now.",
        );
      });
    return () => {
      cancelled = true;
    };
  }, [proofRef, valid]);

  const onVerify = useCallback(async () => {
    setRunning(true);
    setVerifyError(null);
    try {
      setVerify(await reverifyProof(proofRef));
    } catch (e) {
      setVerifyError(
        e instanceof FallbackError
          ? (e.reason ?? "Re-verify is rate limited; try again shortly.")
          : e instanceof NetworkError && e.status === 404
            ? "This proof has expired."
            : "Re-verify failed; try again.",
      );
    } finally {
      setRunning(false);
    }
  }, [proofRef]);

  return (
    <div className="h-screen overflow-y-auto">
      <PageHeader
        sticky
        title="Proof"
        titleAs="span"
        right={
          <>
            <span className="font-mono text-meta text-faint" title={proofRef}>{proofRef.slice(0, 24)}</span>
            <PageNav />
          </>
        }
      />
      {data ? (
        <ProofView data={data} verify={verify} running={running} verifyError={verifyError} onVerify={onVerify} />
      ) : (
        <p className="mx-auto max-w-[1100px] px-4 py-10 sm:px-8 text-body text-faint" role={loadError ? "alert" : undefined}>
          {loadError ?? "Loading the proof."}
        </p>
      )}
    </div>
  );
}
