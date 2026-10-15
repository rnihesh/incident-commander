import { fetchEventSource } from "@microsoft/fetch-event-source";
import { API_URL, FallbackError, NetworkError, type FetchLike } from "./api";
import type { RunEvent } from "./events";

class Fatal extends Error {}
class Retriable extends Error {}

export interface StreamOptions {
  baseUrl?: string;
  fetch?: FetchLike;
  signal?: AbortSignal;
  /** Consecutive failed connection attempts tolerated before giving up. */
  maxRetries?: number;
  retryMs?: number;
  onEvent: (ev: RunEvent) => void;
  onStatus?: (s: "connecting" | "streaming" | "reconnecting") => void;
}

/**
 * Stream RunEvents for a run over SSE. Resolves when the run emits `done` (or
 * the caller aborts); rejects with FallbackError / NetworkError when the stream
 * cannot be established. A stream that closes before `done` is re-opened with
 * the Last-Event-ID of the last delivered event, so the server resumes there.
 */
export async function streamRunEvents(runId: string, o: StreamOptions): Promise<void> {
  const base = o.baseUrl ?? API_URL;
  const maxRetries = o.maxRetries ?? 6;
  const retryMs = o.retryMs ?? 800;
  let finished = false;
  let failures = 0;
  let lastId = -1;
  const ctrl = new AbortController();
  o.signal?.addEventListener("abort", () => ctrl.abort());
  if (o.signal?.aborted) return;

  try {
    await fetchEventSource(`${base}/api/runs/${encodeURIComponent(runId)}/events`, {
      credentials: "include",
      signal: ctrl.signal,
      openWhenHidden: true,
      fetch: o.fetch as typeof fetch | undefined,
      async onopen(res) {
        if (res.ok && (res.headers.get("content-type") ?? "").includes("text/event-stream")) {
          failures = 0;
          o.onStatus?.("streaming");
          return;
        }
        if (res.status === 429 || res.status === 503) throw new Fatal(String(res.status));
        if (res.status >= 400 && res.status < 500) throw new Fatal(String(res.status));
        throw new Retriable(String(res.status));
      },
      onmessage(msg) {
        if (!msg.data) return;
        let ev: RunEvent;
        try {
          ev = JSON.parse(msg.data) as RunEvent;
        } catch {
          return; // malformed frame: skip, never kill the stream
        }
        if (typeof ev?.id !== "number") return;
        if (ev.id <= lastId) return; // transport-level duplicate
        lastId = ev.id;
        if (ev.type === "done") finished = true;
        o.onEvent(ev);
        if (finished) ctrl.abort();
      },
      onclose() {
        if (!finished) throw new Retriable("closed before done");
      },
      onerror(err) {
        if (finished || ctrl.signal.aborted || err instanceof Fatal) throw err;
        if (++failures > maxRetries) throw err;
        o.onStatus?.("reconnecting");
        return retryMs * Math.min(failures, 5);
      },
    });
  } catch (e) {
    if (finished || ctrl.signal.aborted) return;
    if (e instanceof Fatal) {
      const status = Number(e.message);
      if (status === 429 || status === 503) throw new FallbackError(status);
      throw new NetworkError(`event stream refused with ${status}`, status);
    }
    throw new NetworkError(e instanceof Error ? e.message : "event stream failed");
  }
}
