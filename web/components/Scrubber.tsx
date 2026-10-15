"use client";

import { useEffect } from "react";
import { Button } from "./ui";
import {
  BASE_FRAME_MS,
  REPLAY_SPEEDS,
  clampIndex,
  fmtClock,
  fmtOffset,
  keyIndex,
  ticksFor,
  type ReplayFrame,
  type ReplayMarkers,
  type ReplaySpeed,
} from "../lib/replay";

const TICK_STYLE: Record<string, string> = {
  verdict: "bg-verified",
  onset: "bg-symptom",
  alert: "bg-falsified",
};

export interface ScrubberProps {
  frames: ReplayFrame[];
  markers: ReplayMarkers;
  index: number;
  onIndex: (i: number) => void;
  playing: boolean;
  onPlayingChange: (p: boolean) => void;
  speed: ReplaySpeed;
  onSpeed: (s: ReplaySpeed) => void;
}

/** Play/pause, speed, and a draggable timeline with tick marks. Arrow keys step frames. */
export function Scrubber({ frames, markers, index, onIndex, playing, onPlayingChange, speed, onSpeed }: ScrubberProps) {
  const n = frames.length;
  const atEnd = index >= n - 1;

  // Advance on a timer while playing; stop at the last frame.
  useEffect(() => {
    if (!playing || n === 0) return;
    if (atEnd) {
      onPlayingChange(false);
      return;
    }
    const id = setTimeout(() => onIndex(clampIndex(index + 1, n)), BASE_FRAME_MS / speed);
    return () => clearTimeout(id);
  }, [playing, index, n, speed, atEnd, onIndex, onPlayingChange]);

  const ticks = ticksFor(markers, frames);
  // On a phone, two markers within a quarter of the bar would have touching labels: the second
  // of a close pair drops to a lower row (from sm up there is room on one row).
  const lower = ticks.reduce<boolean[]>((acc, t, i) => [...acc, i > 0 && !acc[i - 1] && t.pct - ticks[i - 1].pct < 25], []);
  const frame = frames[clampIndex(index, n)];

  const onKeyDown = (e: React.KeyboardEvent) => {
    const next = keyIndex(e.key, index, n, e.shiftKey);
    if (next === null) return;
    e.preventDefault();
    onPlayingChange(false);
    onIndex(next);
  };

  return (
    <div className="border-t border-rule-hi bg-deck px-5 py-3" role="group" aria-label="Replay controls">
      <div className="flex flex-wrap items-center gap-4">
        <Button
          variant="primary"
          onClick={() => {
            if (atEnd) onIndex(0);
            onPlayingChange(!playing || atEnd);
          }}
          className="w-20"
        >
          {playing ? "Pause" : atEnd ? "Restart" : "Play"}
        </Button>
        <div className="flex h-7 overflow-hidden rounded-ctl border border-rule-hi" role="radiogroup" aria-label="Replay speed">
          {REPLAY_SPEEDS.map((sp) => (
            <button
              key={sp}
              type="button"
              role="radio"
              aria-checked={speed === sp}
              onClick={() => onSpeed(sp)}
              className={`px-2 font-mono text-meta ${speed === sp ? "bg-symptom/15 text-ink" : "text-dim hover:text-ink"}`}
            >
              {sp}x
            </button>
          ))}
        </div>
        {frame && (
          <p className="font-mono text-meta tabular-nums text-dim" aria-live="off">
            {fmtClock(frame.t)} · {fmtOffset(frame.t, markers.alert_t)} · frame {index + 1}/{n}
          </p>
        )}
      </div>

      <div className={`relative mt-3 ${lower.some(Boolean) ? "h-14 sm:h-11" : "h-11"}`}>
        <input
          type="range"
          min={0}
          max={Math.max(n - 1, 0)}
          step={1}
          value={clampIndex(index, n)}
          onChange={(e) => {
            onPlayingChange(false);
            onIndex(clampIndex(Number(e.target.value), n));
          }}
          onKeyDown={onKeyDown}
          aria-label="Replay position"
          aria-valuetext={frame ? `${fmtOffset(frame.t, markers.alert_t)}, frame ${index + 1} of ${n}` : undefined}
          className="absolute inset-x-0 top-0 w-full accent-symptom"
        />
        <ul className="pointer-events-none absolute inset-x-0 top-6" aria-label="Timeline markers">
          {ticks.map((t, i) => (
            // Labels near either end hang inward so they never run off the bar.
            <li
              key={t.key}
              className={`absolute flex flex-col ${t.pct > 90 ? "-translate-x-full items-end" : t.pct < 10 ? "items-start" : "-translate-x-1/2 items-center"}`}
              style={{ left: `${t.pct}%` }}
            >
              <i className={`block w-0.5 ${lower[i] ? "h-5 sm:h-2" : "h-2"} ${TICK_STYLE[t.key]}`} aria-hidden />
              <span className="block whitespace-nowrap text-meta text-dim">{t.label}</span>
            </li>
          ))}
        </ul>
      </div>
    </div>
  );
}
