"use client";

import type { RehearsalData } from "@/lib/payloads";
import type { ConsoleState } from "@/lib/reducer";
import { Badge, Hash, PanelHead, shortSvc } from "./ui";

/** "4 min 28 s" (or "38 s", "2 h 3 min 0 s") for a lead time in seconds. */
export function formatLead(seconds: number): string {
  const s = Math.max(0, Math.round(seconds));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = s % 60;
  if (h) return `${h} h ${m} min ${sec} s`;
  if (m) return `${m} min ${sec} s`;
  return `${sec} s`;
}

export function leadSentence(leadS: number | null | undefined): string {
  if (typeof leadS !== "number" || !Number.isFinite(leadS) || leadS <= 0) {
    return "As true at alert time: the change landed before the earliest symptom.";
  }
  return `As true at alert time: the change landed ${formatLead(leadS)} before the earliest symptom.`;
}

export function Verdict({ s, view }: { s: ConsoleState; view: "true" | "known" }) {
  const v = s.verdict;
  return (
    <section aria-labelledby="verdict-h" className="border-b border-rule">
      <PanelHead
        id="verdict-h"
        title="Verdict"
        right={
          v && (
            <span className="flex items-center gap-2">
              {v.degraded && <Badge tone="degraded">DEGRADED</Badge>}
              <Badge tone={v.outcome === "DIAGNOSED" ? "verified" : v.outcome === "INCONCLUSIVE" ? "drifted" : "falsified"}>{v.outcome}</Badge>
            </span>
          )
        }
      />
      {!v ? (
        <p className="max-w-[72ch] px-inset py-4 text-body text-faint">No verdict yet. A verdict is only accepted after the absence certificate and the necessity check have run.</p>
      ) : (
        <div className="arrive px-inset py-3.5">
          {v.root_cause ? (
            <>
              <p className="font-mono text-display font-medium tracking-tight text-ink">{v.root_cause.change}</p>
              <p className="mt-2 text-body text-ink/90">
                {v.root_cause.summary} on <span className="font-mono">{shortSvc(v.root_cause.service)}</span>
                {v.root_cause.author && <> by {v.root_cause.author}</>}
              </p>
              <p className="mt-1 text-label text-dim">
                {view === "known"
                  ? v.root_cause.known_at_alert
                    ? "As known at alert time: this change was already recorded when the alert fired."
                    : "As known at alert time: not yet recorded. Visible only in the as-true view."
                  : leadSentence(v.root_cause.lead_s)}
              </p>
            </>
          ) : (
            <p className="text-body text-ink/90">{v.headline}</p>
          )}
          {v.root_cause && <p className="mt-2 max-w-[70ch] text-label text-dim">{v.headline}</p>}
          {v.chain_head && (
            <p className="mt-2 flex items-center gap-2 text-meta text-faint">chain head <Hash value={v.chain_head} head={20} /></p>
          )}
        </div>
      )}
    </section>
  );
}

type Item = { key: string; tone: "verified" | "drifted" | "falsified" | "info" | "muted"; tag: string; title: string; body?: string; href?: string };

function rehearsalItem(r: RehearsalData): Item {
  const title = [r.ms ? `Cloned world, ${r.ms} ms` : "Rehearsal", r.checked != null ? `${r.checked} candidates re-checked` : null]
    .filter(Boolean)
    .join(" · ");
  const still = (r.still_explains ?? []).map(
    (x) => `${x.entity_id} still explains ${x.full ? "every symptom" : x.services.map(shortSvc).join(", ")} via ${x.path.map(shortSvc).join(" > ")}`,
  );
  return {
    key: "rehearsal",
    tone: r.result === "CLEARED" ? "verified" : r.result === "REFUSED" ? "falsified" : "muted",
    tag: `REHEARSAL ${r.result}`,
    title,
    body: r.result === "REFUSED" && still.length ? `After rolling back ${r.change ?? "the change"}: ${still.join("; ")}. No PR opened.` : r.detail,
  };
}

export function Response({ s }: { s: ConsoleState }) {
  const items: Item[] = [];
  if (s.handoff) items.push({ key: "handoff", tone: "info", tag: "HANDOFF", title: `To ${s.handoff.to}`, body: s.handoff.summary });
  if (s.rehearsal) items.push(rehearsalItem(s.rehearsal));
  s.actions.forEach((a, i) =>
    items.push({ key: `a${i}`, tone: a.status === "ok" ? "verified" : "falsified", tag: a.kind === "pr" ? "PR" : a.kind === "page" ? "PAGE" : "STATUS", title: a.label, body: a.detail, href: a.url }),
  );
  if (s.memory) items.push({ key: "memory", tone: "info", tag: "MEMORY", title: "Written back to the graph", body: s.memory.detail });
  if (!items.length) return null;
  return (
    <section aria-labelledby="resp-h">
      <PanelHead id="resp-h" title="Response" meta={s.phase ?? undefined} />
      <ol className="divide-y divide-rule/70">
        {items.map((it) => (
          <li key={it.key} className="arrive grid grid-cols-[152px_minmax(0,1fr)] items-baseline gap-3 px-inset py-2">
            <span><Badge tone={it.tone}>{it.tag}</Badge></span>
            <span className="min-w-0 text-label">
              <span className="text-ink">
                {it.href ? (
                  <a href={it.href} target="_blank" rel="noopener noreferrer" className="underline decoration-dotted underline-offset-2 hover:text-symptom">{it.title}</a>
                ) : it.title}
              </span>
              {it.body && <span className="block text-dim">{it.body}</span>}
              {it.href && <span className="block truncate font-mono text-meta text-faint">{it.href}</span>}
            </span>
          </li>
        ))}
      </ol>
    </section>
  );
}
