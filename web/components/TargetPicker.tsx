"use client";

import { useMemo, useState } from "react";
import { JUDGE_FAULT_CLASSES, type JudgeFaultClass, type JudgeTarget, type ServiceChanges } from "@/lib/judge";
import { Badge, Button, Input, PanelHead, Select } from "./ui";

const fmt = (t: number | null | undefined) => (t ? new Date(t * 1000).toISOString().replace("T", " ").slice(0, 16) + "Z" : "unrecorded");

/** Searchable service list for the judge: region/tier, why a target is unusable, recent changes. */
export function TargetPicker({
  targets,
  loading,
  error,
  selected,
  onSelect,
  changes,
  faultClass,
  onFaultClass,
  onInject,
  injecting,
  injectError,
  disabled,
}: {
  targets: JudgeTarget[];
  loading: boolean;
  error: string | null;
  selected: string | null;
  onSelect: (id: string) => void;
  changes: ServiceChanges | null;
  faultClass: JudgeFaultClass;
  onFaultClass: (c: JudgeFaultClass) => void;
  onInject: () => void;
  injecting: boolean;
  injectError: string | null;
  disabled?: boolean;
}) {
  const [q, setQ] = useState("");
  const [onlyUsable, setOnlyUsable] = useState(false);
  const filtered = useMemo(() => {
    const needle = q.trim().toLowerCase();
    return targets.filter(
      (t) =>
        (!needle || [t.id, t.name, t.region, t.domain].some((v) => String(v ?? "").toLowerCase().includes(needle))) &&
        (!onlyUsable || !t.unavailable[faultClass]),
    );
  }, [targets, q, onlyUsable, faultClass]);
  const sel = targets.find((t) => t.id === selected) ?? null;
  const why = sel?.unavailable[faultClass] ?? null;
  const shown = filtered.slice(0, 200);

  return (
    <section aria-labelledby="picker-h" className="@container flex min-h-0 flex-col border border-rule bg-deck">
      <PanelHead
        id="picker-h"
        title="Pick a target"
        meta={loading ? "loading" : `${filtered.length} of ${targets.length} service instances (service × region)`}
        hint="Choose the service to break. The agent is not told which one; dimmed services cannot raise an alert."
      />
      <div className="flex flex-wrap items-center gap-x-3 gap-y-2 border-b border-rule px-inset py-2">
        <label className="sr-only" htmlFor="target-search">Search services</label>
        <Input
          id="target-search"
          type="search"
          value={q}
          onChange={(e) => setQ(e.target.value)}
          placeholder="search name, region, domain"
          className="min-w-0 flex-1 basis-56 placeholder:text-faint"
        />
        <label className="flex items-center gap-1.5 text-meta text-dim">
          <input type="checkbox" checked={onlyUsable} onChange={(e) => setOnlyUsable(e.target.checked)} className="accent-symptom" />
          usable for {faultClass}
        </label>
      </div>
      {error && <p role="alert" className="px-inset py-2 text-label text-falsified">{error}</p>}
      <div className="grid min-h-0 flex-1 grid-cols-1 @min-[440px]:grid-cols-[minmax(0,1.3fr)_minmax(0,1fr)]">
        <ul role="listbox" aria-label="Services" className="max-h-[46vh] min-h-0 overflow-y-auto border-b border-rule @min-[440px]:border-b-0 @min-[440px]:border-r">
          {shown.map((t) => {
            const reason = t.unavailable[faultClass];
            const active = t.id === selected;
            return (
              <li key={t.id} role="option" aria-selected={active} aria-disabled={!!reason}>
                <button
                  type="button"
                  onClick={() => onSelect(t.id)}
                  title={reason ?? `${t.upstream_services} services upstream within 3 hops`}
                  className={`flex w-full flex-col items-start gap-0.5 border-b border-rule/60 px-3 py-1.5 text-left ${active ? "bg-symptom/15" : "hover:bg-raise"} ${reason ? "opacity-50" : ""}`}
                >
                  <span className="flex w-full items-baseline gap-2">
                    <span className="truncate font-mono text-label text-ink">{t.name}</span>
                    <span className="shrink-0 whitespace-nowrap font-mono text-meta text-dim">{t.region}</span>
                    <span className="ml-auto shrink-0 whitespace-nowrap font-mono text-meta text-faint">tier {t.tier}</span>
                  </span>
                  {reason && <span className="text-meta text-drifted">{reason}</span>}
                </button>
              </li>
            );
          })}
          {filtered.length > shown.length && <li className="px-3 py-2 text-meta text-faint">{filtered.length - shown.length} more: refine the search.</li>}
          {!loading && !filtered.length && <li className="px-3 py-3 text-label text-faint">No service matches.</li>}
        </ul>
        <div className="min-h-0 overflow-y-auto px-inset py-3">
          {!sel ? (
            <p className="text-body text-faint">Select a service from the list or click a node on the map.</p>
          ) : (
            <>
              <p className="font-mono text-body text-ink">{sel.id}</p>
              <p className="mt-0.5 text-label text-dim">
                {sel.domain} · {sel.region} · tier {sel.tier} · {sel.upstream_services} services upstream within 3 hops
              </p>
              <div className="mt-2">
                {why ? <Badge tone="drifted">NO ALERT PATH</Badge> : <Badge tone="verified">ALERT PATH OK</Badge>}
                {why && <p className="mt-1 text-label text-drifted">{why}</p>}
              </div>
              <h3 className="mt-3 text-label font-semibold text-ink">Recent changes</h3>
              {!changes || changes.service_id !== sel.id ? (
                <p className="text-label text-faint">loading</p>
              ) : changes.changes.length === 0 ? (
                <p className="text-label text-faint">No changes recorded on this service.</p>
              ) : (
                <ul className="mt-1 space-y-1">
                  {changes.changes.slice(0, 8).map((c) => (
                    <li key={c.id} className="text-meta">
                      <span className="font-mono text-ink">{c.id}</span> <span className="text-dim">{c.kind}</span>{" "}
                      <span className="text-faint">{fmt(c.valid_from)}</span>
                      <span className="block truncate text-dim">{c.summary}</span>
                    </li>
                  ))}
                </ul>
              )}
            </>
          )}
        </div>
      </div>
      <div className="flex flex-wrap items-center gap-2 border-t border-rule px-inset py-2.5">
        <label className="text-meta text-dim" htmlFor="judge-class">Fault class</label>
        <Select
          id="judge-class"
          value={faultClass}
          onChange={(e) => onFaultClass(e.target.value as JudgeFaultClass)}
          mono={false}
        >
          {JUDGE_FAULT_CLASSES.map((c) => <option key={c.id} value={c.id}>{c.label}</option>)}
        </Select>
        <Button
          variant="primary"
          onClick={onInject}
          disabled={disabled || injecting || !sel || !!why}
          title={why ?? undefined}
          className="ml-auto"
        >
          {injecting ? "Committing" : "Commit and inject blind"}
        </Button>
        {injectError && <p role="alert" className="w-full text-label text-falsified">{injectError}</p>}
      </div>
    </section>
  );
}
