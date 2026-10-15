"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { RunEvent } from "./events";
import { initialState, reduce, type ConsoleState } from "./reducer";
import { getRecordedHero } from "./api";
import { streamRunEvents } from "./sse";

export const SPEEDS = [0.5, 1, 2, 4, 8] as const;

export interface RunStream {
  state: ConsoleState;
  cursor: number;
  total: number;
  playing: boolean;
  speed: number;
  setSpeed: (s: number) => void;
  play: () => void;
  pause: () => void;
  restart: () => void;
  seek: (n: number) => void;
  finish: () => void;
  /** "mock" and "recorded" replay an array; "live" follows an SSE run. */
  kind: "mock" | "live" | "recorded";
  /** Live connection status (live only). */
  status: "idle" | "connecting" | "streaming" | "reconnecting" | "closed" | "error";
  error: string | null;
  /** True while a run is in flight (live stream open, or replay still playing). */
  active: boolean;
}

export type StreamSource =
  | { kind: "mock"; events: RunEvent[] }
  /** runId null = nothing started; recorded = backend refused, replay /api/recorded/hero. */
  | { kind: "live"; runId: string | null; recorded?: boolean };

/**
 * Mock player: replays a recorded event array with the original inter-event
 * timing (gaps capped so long LLM pauses do not stall a demo). State is derived
 * from the first `cursor` events through the pure reducer, so seeking is free.
 * The SSE source (U9) replaces the timer with an append-only events array and
 * keeps the same return shape.
 */
function useReplay(events: RunEvent[], opts: { autoplay?: boolean; initialSpeed?: number } = {}) {
  const [cursor, setCursor] = useState(0);
  const [playing, setPlaying] = useState(opts.autoplay ?? true);
  const [speed, setSpeed] = useState(opts.initialSpeed ?? 1);
  const total = events.length;
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const [prevEvents, setPrevEvents] = useState(events);
  if (prevEvents !== events) {
    // Source swapped (e.g. recorded run finished loading): restart from the top.
    setPrevEvents(events);
    setCursor(0);
    setPlaying(opts.autoplay ?? true);
  }

  const state = useMemo(() => {
    let s = initialState;
    for (let i = 0; i < cursor; i++) s = reduce(s, events[i]);
    return s;
  }, [events, cursor]);

  useEffect(() => {
    if (!playing || cursor >= total) return;
    const prev = cursor > 0 ? events[cursor - 1].ts : events[0].ts;
    const gap = Math.min(Math.max(events[cursor].ts - prev, 60), 1600) / speed;
    timer.current = setTimeout(() => setCursor((c) => Math.min(c + 1, total)), cursor === 0 ? 250 : gap);
    return () => {
      if (timer.current) clearTimeout(timer.current);
    };
  }, [playing, cursor, total, events, speed]);

  const play = useCallback(() => {
    setCursor((c) => (c >= total ? 0 : c));
    setPlaying(true);
  }, [total]);
  const pause = useCallback(() => setPlaying(false), []);
  const restart = useCallback(() => {
    setCursor(0);
    setPlaying(true);
  }, []);
  const seek = useCallback((n: number) => setCursor(Math.max(0, Math.min(total, n))), [total]);
  const finish = useCallback(() => {
    setCursor(total);
    setPlaying(false);
  }, [total]);

  return { state, cursor, total, playing: playing && cursor < total, speed, setSpeed, play, pause, restart, seek, finish };
}

const NOOP = () => {};
const NO_EVENTS: RunEvent[] = [];

/** Live SSE follower: appends events through the same reducer. Remount to switch runs. */
function useLive(runId: string | null) {
  const [state, setState] = useState<ConsoleState>(initialState);
  const [count, setCount] = useState(0);
  const [status, setStatus] = useState<RunStream["status"]>("idle");
  const [error, setError] = useState<string | null>(null);
  const [fellBack, setFellBack] = useState(false);

  useEffect(() => {
    if (!runId) return;
    const ctrl = new AbortController();
    streamRunEvents(runId, {
      signal: ctrl.signal,
      onStatus: (st) => setStatus(st),
      onEvent: (ev) => {
        setState((s) => reduce(s, ev));
        setCount((c) => c + 1);
      },
    }).then(
      () => { if (!ctrl.signal.aborted) setStatus("closed"); },
      (e: unknown) => {
        if (ctrl.signal.aborted) return;
        setStatus("error");
        setError(e instanceof Error ? e.message : "stream failed");
        if (e instanceof Error && e.name !== "NetworkError") setFellBack(true);
      },
    );
    return () => ctrl.abort();
  }, [runId]);

  return { state, count, status: runId && status === "idle" ? ("connecting" as const) : status, error, fellBack };
}

/** Recorded fallback: fetch the saved hero stream once. */
function useRecorded(enabled: boolean) {
  const [events, setEvents] = useState<RunEvent[]>(NO_EVENTS);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    if (!enabled) return;
    let alive = true;
    getRecordedHero().then(
      (evs) => {
        if (alive) setEvents(evs.map((e) => (e.type === "run_started" ? { ...e, data: { ...e.data, recorded: true } } : e)));
      },
      (e: unknown) => { if (alive) setError(e instanceof Error ? e.message : "could not load recorded run"); },
    );
    return () => { alive = false; };
  }, [enabled]);
  return { events, error };
}

/**
 * Unified run source. `mock` replays a fixture (demo scenarios); `live` follows
 * an SSE run, and when the backend refuses it plays the recorded hero run with
 * the existing replay player. Everything flows through the same reducer.
 * Passing a bare RunEvent[] is shorthand for a mock source.
 */
export function useRunStream(
  source: StreamSource | RunEvent[],
  opts: { autoplay?: boolean; initialSpeed?: number } = {},
): RunStream {
  const src: StreamSource = Array.isArray(source) ? { kind: "mock", events: source } : source;
  const runId = src.kind === "live" ? src.runId : null;
  const live = useLive(runId);
  const useRec = src.kind === "live" && (!!src.recorded || (runId !== null && live.fellBack));
  const rec = useRecorded(useRec);
  const replayEvents = src.kind === "mock" ? src.events : useRec ? rec.events : NO_EVENTS;
  const replay = useReplay(replayEvents, opts);

  if (src.kind === "live" && !useRec) {
    return {
      state: live.state,
      cursor: live.count,
      total: live.count,
      playing: false,
      speed: 1,
      setSpeed: NOOP,
      play: NOOP,
      pause: NOOP,
      restart: NOOP,
      seek: NOOP,
      finish: NOOP,
      kind: "live",
      status: live.status,
      error: live.error,
      active: runId !== null && !live.state.done && live.status !== "error" && live.status !== "closed",
    };
  }
  const kind = src.kind === "mock" ? "mock" : "recorded";
  return {
    ...replay,
    kind,
    status: "closed",
    error: rec.error,
    active: kind === "recorded" && !rec.error && (replayEvents.length === 0 || replay.cursor < replay.total),
  };
}
