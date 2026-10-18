"use client";

import { useEffect, useRef, useState } from "react";
import type { VerifyResponse } from "@/lib/api";
import {
  EMPTY_TAMPER_FORM,
  buildTamperPayload,
  describeTamper,
  flipText,
  flippedProofs,
  fragilityMap,
  fragilityStale,
  tamperFormFor,
  trySuggestions,
  type FragilityResponse,
  type TamperAction,
  type TamperForm,
  type TamperKind,
  type TamperResponse,
} from "../lib/judge";
import { Badge, Button, Cypher, Input, PanelHead, Select, type Tone } from "./ui";

const KINDS: { id: TamperKind; label: string }[] = [
  { id: "add_deploy_upstream", label: "Add deploy upstream" },
  { id: "remove_edge", label: "Remove edge" },
  { id: "change_timestamp", label: "Change timestamp" },
];
const STATUS_TONE: Record<string, Tone> = {
  VERIFIED: "verified", DRIFTED: "drifted", UNANCHORED: "info", FALSIFIED: "falsified", TAMPERED: "tampered",
};

const field = "mt-1 w-full";

/** Keeps a pre-filled value visible in a select even when its option list lacks it. */
function withCurrent<T extends { value: string }>(opts: T[], value: string, make: (v: string) => T): T[] {
  return !value || opts.some((o) => o.value === value) ? opts : [...opts, make(value)];
}

/**
 * Three parameterized edits of the judge's own world (add a deploy upstream, remove a
 * dependency edge, move a change in time), each confirmed before it is written and
 * described exactly after; then Verify and Re-derive. "Try these" lists the single edits
 * the fragility map found would flip the verdict (tried on throwaway copies of the
 * snapshot) and pre-fills the form with one.
 */
export function TamperPanel({
  incidentId,
  onset,
  services,
  edges,
  changes,
  selectedNode,
  disabled,
  onSubmit,
  log,
  onVerify,
  verifying,
  verify,
  onRederive,
  rederiving,
  onFocusNode,
  initialFragility = null,
  loadFragility,
  runId = null,
}: {
  /** The round's current run; a re-derive changes it and clears the fragility map. */
  runId?: string | null;
  incidentId: string;
  onset: number | null;
  services: { id: string; label: string }[];
  edges: { source: string; target: string }[];
  changes: { id: string; service_id: string; kind: string }[];
  selectedNode: string | null;
  disabled?: boolean;
  onSubmit: (a: TamperAction) => Promise<TamperResponse>;
  log: TamperResponse[];
  onVerify: () => void;
  verifying: boolean;
  verify: VerifyResponse | null;
  onRederive: () => void;
  rederiving: boolean;
  onFocusNode: (id: string) => void;
  /** A fragility map already computed (tests, or a parent that fetched it). */
  initialFragility?: FragilityResponse | null;
  /** Defaults to POST /api/judge/{incident}/fragility. */
  loadFragility?: (incidentId: string) => Promise<FragilityResponse>;
}) {
  const [kind, setKind] = useState<TamperKind>("add_deploy_upstream");
  const [form, setForm] = useState<TamperForm>(EMPTY_TAMPER_FORM);
  const [pending, setPending] = useState<TamperAction | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [fragility, setFragility] = useState<{ map: FragilityResponse; epoch: number } | null>(
    initialFragility ? { map: initialFragility, epoch: 0 } : null,
  );
  const [mapping, setMapping] = useState(false);
  const [mapError, setMapError] = useState<string | null>(null);
  // Bumped by every successful tamper and every new run: a map from an older epoch describes a world
  // that no longer exists, so it is cleared, or labelled stale if it finished after the bump.
  const [epoch, setEpoch] = useState(0);
  const epochRef = useRef(0);
  const bumpEpoch = () => {
    epochRef.current += 1;
    setEpoch(epochRef.current);
    setFragility(null);
  };
  const lastRun = useRef(runId);
  useEffect(() => {
    if (lastRun.current === runId) return;
    lastRun.current = runId;
    epochRef.current += 1;
    setEpoch(epochRef.current);
    setFragility(null);
  }, [runId]);
  const stale = fragility !== null && fragilityStale(fragility.epoch, epoch);
  const suggestions = trySuggestions(fragility?.map ?? null);
  const mapFragility = async () => {
    const started = epochRef.current;
    setMapping(true);
    setMapError(null);
    try {
      const map = await (loadFragility ?? ((iid: string) => fragilityMap(iid)))(incidentId);
      setFragility({ map, epoch: started });
    } catch (e) {
      setMapError(e instanceof Error ? e.message : "fragility map failed");
    } finally {
      setMapping(false);
    }
  };
  const prefill = (a: TamperAction) => {
    const next = tamperFormFor(a, onset);
    setKind(next.kind);
    setForm(next.form);
    setPending(null);
    setError(null);
  };
  const set = (patch: Partial<TamperForm>) => {
    setForm((f) => ({ ...f, ...patch }));
    setPending(null);
    setError(null);
  };
  // A canvas click pre-fills the service field until the judge edits it.
  const serviceId = form.service_id || selectedNode || "";
  const flipped = flippedProofs(verify);

  const review = () => {
    const p = buildTamperPayload(kind, { ...form, service_id: serviceId }, onset);
    if ("error" in p) {
      setError(p.error);
      setPending(null);
    } else {
      setError(null);
      setPending(p);
    }
  };
  const confirm = async () => {
    if (!pending) return;
    setBusy(true);
    try {
      await onSubmit(pending);
      setPending(null);
      bumpEpoch();
    } catch (e) {
      setError(e instanceof Error ? e.message : "tamper failed");
    } finally {
      setBusy(false);
    }
  };

  return (
    <section aria-labelledby="tamper-h" className="border-b border-rule">
      <PanelHead id="tamper-h" title="Tamper" meta={`changes only your sandbox's copy of the graph, ${incidentId}`} />
      <div className="space-y-2 border-b border-rule px-inset py-2.5" aria-label="Try these">
        <div className="flex flex-wrap items-center gap-2">
          <span className="text-label font-semibold text-ink">Try these</span>
          <Button size="sm" onClick={() => void mapFragility()} disabled={disabled || mapping}>
            {mapping ? "Trying edits on copies" : fragility ? "Map again" : "Find edits that flip the verdict"}
          </Button>
        </div>
        {mapError && <p role="alert" className="text-label text-falsified">{mapError}</p>}
        {fragility && (
          <p className="text-meta text-dim">
            {stale && <Badge tone="drifted">stale</Badge>}{stale && " Computed before your latest edit; map again. "}
            {fragility.map.applicable ? fragility.map.detail : `Nothing to flip: ${fragility.map.detail}`}
            {fragility.map.copy_ms !== null && <span className="text-faint"> · ~{Math.round(fragility.map.copy_ms)} ms per copy</span>}
            {fragility.map.out_of_time && <span className="text-faint"> · time budget reached, {fragility.map.skipped} edit(s) not tried</span>}
          </p>
        )}
        {suggestions.length > 0 && (
          <ul className="space-y-1">
            {suggestions.map((e, i) => (
              <li key={i} className="flex flex-wrap items-baseline gap-x-2 text-label">
                <button
                  type="button"
                  onClick={() => prefill(e.tamper)}
                  disabled={disabled}
                  className="text-left text-ink underline decoration-dotted underline-offset-2 hover:text-tampered disabled:opacity-40"
                >
                  {e.label}
                </button>
                <Badge tone={e.verdict === "RIVAL" ? "drifted" : "falsified"}>{flipText(e)}</Badge>
                {e.prediction.sources && e.prediction.sources.length > 1 && (
                  <span className="text-meta text-faint">removes {e.prediction.sources.join(" + ")}</span>
                )}
              </li>
            ))}
          </ul>
        )}
      </div>
      <div className="flex gap-1 border-b border-rule px-3 pt-1" role="tablist" aria-label="Tamper action">
        {KINDS.map((k) => (
          <button
            key={k.id}
            type="button"
            role="tab"
            aria-selected={kind === k.id}
            onClick={() => { setKind(k.id); setPending(null); setError(null); }}
            className={`-mb-px h-8 border-b-2 px-2.5 text-label font-medium ${kind === k.id ? "border-tampered text-ink" : "border-transparent text-dim hover:text-ink"}`}
          >
            {k.label}
          </button>
        ))}
      </div>
      <div className="space-y-3 px-inset py-3">
        {kind === "add_deploy_upstream" && (
          <>
            <label className="block text-meta text-dim">
              Service (upstream of the alerting service; click a node to pick)
              <Select value={serviceId} onChange={(e) => set({ service_id: e.target.value })} className={field}>
                <option value="">choose a service</option>
                {withCurrent(services.map((s) => ({ value: s.id, label: s.label })), serviceId, (v) => ({ value: v, label: v })).map((s) => (
                  <option key={s.value} value={s.value}>{s.label}</option>
                ))}
              </Select>
            </label>
            <label className="block text-meta text-dim">
              Minutes before the earliest onset
              <Input inputMode="numeric" value={form.minutes_before_onset} onChange={(e) => set({ minutes_before_onset: e.target.value })} className={field} />
            </label>
            <label className="flex items-center gap-2 text-meta text-dim">
              <input type="checkbox" className="accent-symptom" checked={form.as_if_known_at_alert} onChange={(e) => set({ as_if_known_at_alert: e.target.checked })} />
              Recorded before the alert (unchecked: late-arriving, only the as-true view sees it)
            </label>
          </>
        )}
        {kind === "remove_edge" && (
          <label className="block text-meta text-dim">
            Dependency (caller depends on callee)
            <Select
              value={form.from_service_id && form.to_service_id ? `${form.from_service_id}|${form.to_service_id}` : ""}
              onChange={(e) => {
                const [from, to] = e.target.value.split("|");
                set({ from_service_id: from ?? "", to_service_id: to ?? "" });
              }}
              className={field}
            >
              <option value="">choose an edge</option>
              {withCurrent(
                // Graph edges run in the IMPACTS direction (callee -> caller).
                edges.map((e) => ({ value: `${e.target}|${e.source}`, label: `${e.target} depends on ${e.source}` })),
                form.from_service_id && form.to_service_id ? `${form.from_service_id}|${form.to_service_id}` : "",
                (v) => ({ value: v, label: v.split("|").join(" depends on ") }),
              ).map((o) => (
                <option key={o.value} value={o.value}>{o.label}</option>
              ))}
            </Select>
          </label>
        )}
        {kind === "change_timestamp" && (
          <>
            <label className="block text-meta text-dim">
              Change
              <Select value={form.change_id} onChange={(e) => set({ change_id: e.target.value })} className={field}>
                <option value="">choose a change</option>
                {withCurrent(changes.map((c) => ({ value: c.id, label: `${c.id} · ${c.kind} on ${c.service_id}` })), form.change_id, (v) => ({ value: v, label: v })).map((c) => (
                  <option key={c.value} value={c.value}>{c.label}</option>
                ))}
              </Select>
            </label>
            <label className="block text-meta text-dim">
              New time, minutes relative to the earliest onset (negative = before)
              <Input inputMode="numeric" value={form.shift_minutes} onChange={(e) => set({ shift_minutes: e.target.value })} className={field} />
            </label>
          </>
        )}
        <div className="flex flex-wrap items-center gap-2">
          {!pending ? (
            <Button onClick={review} disabled={disabled}>
              Review edit
            </Button>
          ) : (
            <>
              <Button variant="danger" onClick={() => void confirm()} disabled={disabled || busy}>
                {busy ? "Writing" : "Confirm and write"}
              </Button>
              <Button variant="ghost" onClick={() => setPending(null)}>Cancel</Button>
            </>
          )}
          {disabled && <span className="text-meta text-faint">Available once the run has finished.</span>}
        </div>
        {pending && <p className="text-label text-ink" data-testid="tamper-preview">{describeTamper(pending)}</p>}
        {error && <p role="alert" className="text-label text-falsified">{error}</p>}
      </div>

      {log.length > 0 && (
        <ol className="space-y-2 border-t border-rule px-inset py-3" aria-label="Edits applied">
          {log.map((t, i) => (
            <li key={i} className="text-label">
              <p className="text-ink"><Badge tone="tampered">CHANGED</Badge> <span className="ml-1">{t.summary}</span></p>
              <details className="mt-1">
                <summary className="cursor-pointer text-meta text-dim hover:text-ink">Exact Cypher on {t.graph}</summary>
                {t.statements.map((s, j) => <Cypher key={j} text={s.cypher} params={s.params} />)}
              </details>
            </li>
          ))}
        </ol>
      )}

      <div className="flex flex-wrap items-center gap-2 border-t border-rule px-inset py-2.5">
        <Button onClick={onVerify} disabled={disabled || verifying}>
          {verifying ? "Re-running proofs" : "Verify"}
        </Button>
        <Button onClick={onRederive} disabled={disabled || rederiving}>
          {rederiving ? "Starting" : "Re-derive"}
        </Button>
        {verify && <Badge tone={STATUS_TONE[verify.overall] ?? "muted"} size="lg">{verify.overall}</Badge>}
      </div>
      {verify && (
        <div className="px-inset pb-3">
          {flipped.length === 0 ? (
            <p className="text-label text-dim">No proof flipped to FALSIFIED.</p>
          ) : (
            <ul className="space-y-1" aria-label="Falsified proofs">
              {flipped.map((p) => (
                <li key={p.seq} className="text-label">
                  <Badge tone="falsified">FALSIFIED</Badge>{" "}
                  <span className="font-mono text-ink">#{p.seq} {p.kind}</span> <span className="text-dim">on {p.entity_id}</span>
                  {p.responsible.length > 0 && <span className="text-dim"> responsible </span>}
                  {p.responsible.slice(0, 4).map((n) => (
                    <button key={n} type="button" onClick={() => onFocusNode(n)} className="mr-1.5 font-mono text-falsified underline decoration-dotted underline-offset-2">
                      {n}
                    </button>
                  ))}
                  {p.late && <span className="ml-1 text-late">late-arriving</span>}
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
    </section>
  );
}
