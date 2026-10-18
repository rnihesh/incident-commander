// Ask the graph (R1-R5): the answer from POST /api/ask, read defensively, plus the
// helpers the panel uses (question checks, the answer's status, example questions).

import { limitNotice } from "./limits";

export const ASK_MAX = 500;

/** Questions that answer well on the seeded company, with or without a model. */
export const EXAMPLE_QUESTIONS = [
  "Who owns checkout and who is on call?",
  "Which changes caused incidents last week?",
  "What did we decide after the last config-service outage?",
  "Who changed the rate limiter recently?",
];

export type AskMode = "llm" | "limited";

export interface AskCitation {
  id: string;
  kind: string;
  label: string;
}

export interface AskCall {
  tool: string;
  args: Record<string, unknown>;
  cypher: string[];
  params: Record<string, unknown>[];
  ms: number;
}

export interface AskResponse {
  answer: string;
  mode: AskMode;
  /** At least one cited node was touched by a query in this answer. */
  grounded: boolean;
  /** False when the graph has no information about the question. */
  found: boolean;
  citations: AskCitation[];
  calls: AskCall[];
  ms: number;
  tokens: number;
  /** Cited ids no query touched (dropped by the server's grounding guard). */
  dropped: string[];
  /** Why the answer is in limited mode. */
  note: string | null;
}

/** grounded: sources back it; unverified: it asserts facts no query backs; no_info: nothing found. */
export type AskStatus = "grounded" | "unverified" | "no_info";

export function askStatus(r: Pick<AskResponse, "grounded" | "found">): AskStatus {
  if (r.grounded) return "grounded";
  return r.found ? "unverified" : "no_info";
}

export const STATUS_LABEL: Record<AskStatus, string> = {
  grounded: "grounded",
  unverified: "unverified",
  no_info: "no information",
};

export const MODE_LABEL: Record<AskMode, string> = { llm: "LLM", limited: "limited mode" };

/** Why a question cannot be sent, or null when it can. */
export function checkQuestion(q: string): string | null {
  const t = q.trim();
  if (!t) return "Type a question first.";
  if (t.length > ASK_MAX) return `Questions are limited to ${ASK_MAX} characters (this one has ${t.length}).`;
  return null;
}

const rec = (v: unknown): Record<string, unknown> | null =>
  v && typeof v === "object" && !Array.isArray(v) ? (v as Record<string, unknown>) : null;
const str = (v: unknown, d = ""): string => (typeof v === "string" ? v : d);
const num = (v: unknown): number => (typeof v === "number" && Number.isFinite(v) ? v : 0);
const strs = (v: unknown): string[] => (Array.isArray(v) ? v.filter((x): x is string => typeof x === "string") : []);

/** The panel's view of an answer; null when the body is not recognisable. */
export function parseAsk(raw: unknown): AskResponse | null {
  const b = rec(raw);
  if (!b || typeof b.answer !== "string") return null;
  const citations = (Array.isArray(b.citations) ? b.citations : [])
    .map(rec)
    .filter((c): c is Record<string, unknown> => !!c && typeof c.id === "string" && !!c.id)
    .map((c) => ({ id: c.id as string, kind: str(c.kind, "Node"), label: str(c.label) || (c.id as string) }));
  const calls = (Array.isArray(b.calls) ? b.calls : [])
    .map(rec)
    .filter((c): c is Record<string, unknown> => !!c && typeof c.tool === "string")
    .map((c) => ({
      tool: c.tool as string,
      args: rec(c.args) ?? {},
      cypher: strs(c.cypher),
      params: (Array.isArray(c.params) ? c.params : []).map((p) => rec(p) ?? {}),
      ms: num(c.ms),
    }));
  const grounded = b.grounded === true && citations.length > 0;
  return {
    answer: b.answer,
    mode: b.mode === "llm" ? "llm" : "limited",
    grounded,
    found: b.found !== false,
    citations: grounded ? citations : [],
    calls,
    ms: num(b.ms),
    tokens: num(b.tokens),
    dropped: strs(b.dropped),
    note: typeof b.note === "string" && b.note ? b.note : null,
  };
}

/** "find_entities(search: "checkout")": a call's tool and arguments on one line. */
export function callLabel(c: Pick<AskCall, "tool" | "args">): string {
  const args = Object.entries(c.args)
    .map(([k, v]) => `${k}: ${JSON.stringify(v)}`)
    .join(", ");
  return `${c.tool}(${args})`;
}

/** Query count, time and (when a model ran) tokens, for the answer's meta line. */
export function answerMeta(r: Pick<AskResponse, "calls" | "ms" | "tokens">): string {
  const queries = r.calls.reduce((n, c) => n + c.cypher.length, 0);
  const parts = [`${queries} ${queries === 1 ? "query" : "queries"}`, `${Math.round(r.ms)} ms`];
  if (r.tokens > 0) parts.push(`${r.tokens.toLocaleString("en-US")} tokens`);
  return parts.join(" · ");
}

// --- the chat panel: one conversation per browser tab, kept across runs ------------------

export type ChatMessage =
  | { id: number; role: "user"; text: string }
  | { id: number; role: "assistant"; result: AskResponse }
  | { id: number; role: "error"; text: string };

/** sessionStorage key for the conversation (this tab only, like the sandbox session). */
export const CHAT_KEY = "ic.ask.chat";
/** Messages kept: older ones drop off the top. */
export const CHAT_KEEP = 40;

/** Starter questions: about the visitor's incident first when there is one, then the company. */
export function starterQuestions(i: { incidentId: string | null; rootCause: string | null }): string[] {
  const about = i.incidentId
    ? [i.rootCause ? `Why did you blame ${i.rootCause}?` : "Why did you blame this change?", "What got ruled out?"]
    : [];
  return [...about, ...EXAMPLE_QUESTIONS].slice(0, about.length ? 5 : 4);
}

/** The stored conversation, or [] when it is missing or not recognisable. */
export function readChat(raw: string | null): ChatMessage[] {
  if (!raw) return [];
  try {
    const v: unknown = JSON.parse(raw);
    if (!Array.isArray(v)) return [];
    return v.flatMap((m): ChatMessage[] => {
      const r = rec(m);
      if (!r || typeof r.id !== "number") return [];
      if ((r.role === "user" || r.role === "error") && typeof r.text === "string") return [{ id: r.id, role: r.role, text: r.text }];
      if (r.role === "assistant") {
        const result = parseAsk(r.result);
        return result ? [{ id: r.id, role: "assistant", result }] : [];
      }
      return [];
    });
  } catch {
    return [];
  }
}

export function keepChat(messages: ChatMessage[]): ChatMessage[] {
  return messages.length > CHAT_KEEP ? messages.slice(messages.length - CHAT_KEEP) : messages;
}

/** Plain text for a refused question (question or sandbox limits) or a failed request. */
export function askErrorText(reason: string | undefined, fallback: string): string {
  const r = (reason ?? "").toLowerCase();
  const n = /limit of (\d+) questions per hour/.exec(r)?.[1];
  if (n) return `You have asked ${n} questions in the last hour, the most allowed per sandbox. Try again later.`;
  const notice = limitNotice(reason);
  if (notice) return notice.detail;
  return reason ? `${reason[0].toUpperCase()}${reason.slice(1)}. Try again later.` : fallback;
}
