"use client";

import type { Banner } from "@/lib/reducer";
import { Glyph, TONE, type Tone } from "./ui";

const MAP: Record<Banner["tone"], Tone> = {
  info: "info",
  ok: "verified",
  warn: "drifted",
  bad: "falsified",
  muted: "muted",
  degraded: "degraded",
};

export function StateBanner({ banners }: { banners: Banner[] }) {
  return (
    <div role="status" aria-live="polite" className="flex flex-col">
      {banners.map((b) => {
        const t = TONE[MAP[b.tone]];
        return (
          <div key={b.kind} data-banner={b.kind} className={`flex items-baseline gap-3 border-b px-5 py-1.5 text-label ${t.border} ${t.bg}`}>
            <span className={`flex shrink-0 items-center gap-2 self-center font-semibold ${t.text}`}>
              <Glyph tone={MAP[b.tone]} />
              {b.title}
            </span>
            <span className="min-w-0 text-ink/80">{b.body}</span>
          </div>
        );
      })}
    </div>
  );
}
