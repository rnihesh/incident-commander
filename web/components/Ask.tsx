"use client";

import { useCallback, useEffect, useId, useRef, useState, type FormEvent } from "react";
import { FallbackError, askGraph } from "../lib/api";
import {
  ASK_MAX,
  CHAT_KEY,
  MODE_LABEL,
  STATUS_LABEL,
  answerMeta,
  askErrorText,
  askStatus,
  callLabel,
  checkQuestion,
  keepChat,
  parseAsk,
  readChat,
  starterQuestions,
  type AskResponse,
  type AskStatus,
  type ChatMessage,
} from "../lib/ask";
import { Badge, Button, Chip, Cypher, type Tone } from "./ui";

const STATUS_TONE: Record<AskStatus, Tone> = { grounded: "verified", unverified: "drifted", no_info: "muted" };
const STATUS_FRAME: Record<AskStatus, string> = {
  grounded: "border-verified/40",
  unverified: "border-drifted/50 bg-drifted/5",
  no_info: "border-rule-hi",
};
const STATUS_TITLE: Record<AskStatus, string> = {
  grounded: "Every source below was touched by one of the queries that produced this answer",
  unverified: "None of the cited nodes was touched by a query: treat this answer as unverified",
  no_info: "The graph has nothing to answer this with",
};

/** One answer: badges, the text, its source nodes (pressable when on the graph) and the
 *  Cypher of every tool call behind it. */
export function AskAnswer({
  result,
  inGraph,
  onFocus,
}: {
  result: AskResponse;
  inGraph: (id: string) => boolean;
  onFocus: (ids: string[]) => void;
}) {
  const status = askStatus(result);
  const queries = result.calls.reduce((n, c) => n + c.cypher.length, 0);
  return (
    <article aria-label="Answer" className={`arrive space-y-2.5 border bg-raise p-3 ${STATUS_FRAME[status]}`}>
      <div className="flex flex-wrap items-center gap-2">
        <Badge tone={result.mode === "llm" ? "info" : "degraded"} title={result.mode === "llm" ? "Answered by the model from read-only tool calls" : "Answered by the deterministic router, no model"}>
          {MODE_LABEL[result.mode]}
        </Badge>
        <Badge tone={STATUS_TONE[status]} title={STATUS_TITLE[status]}>{STATUS_LABEL[status]}</Badge>
        <span className="ml-auto font-mono text-meta tabular-nums text-faint">{answerMeta(result)}</span>
      </div>
      <p className="max-w-[80ch] whitespace-pre-line text-body leading-[1.55] text-ink">{result.answer}</p>
      {result.note && <p className="text-meta text-dim">{result.note}</p>}
      {result.citations.length > 0 && (
        <div className="flex flex-wrap items-center gap-1.5">
          <span className="mr-1 text-meta text-dim">Sources</span>
          {result.citations.map((c) => {
            const on = inGraph(c.id);
            return (
              <Chip
                key={c.id}
                mono={false}
                onClick={on ? () => onFocus([c.id]) : undefined}
                title={on ? `Focus ${c.id} on the graph` : `${c.id} (not on the current graph)`}
              >
                <span className="text-faint">{c.kind}</span>
                <span className={on ? "text-ink" : ""}>{c.label}</span>
              </Chip>
            );
          })}
        </div>
      )}
      {result.dropped.length > 0 && (
        <p className="text-meta text-drifted">
          Dropped {result.dropped.length === 1 ? "a citation" : `${result.dropped.length} citations`} no query touched:{" "}
          <span className="font-mono">{result.dropped.join(", ")}</span>
        </p>
      )}
      {queries > 0 && (
        <details>
          <summary className="cursor-pointer font-mono text-meta text-dim hover:text-ink">Show queries ({queries})</summary>
          <ol className="mt-2 space-y-2">
            {result.calls.map((c, i) => (
              <li key={i} className="space-y-1">
                <p className="break-all font-mono text-meta text-dim">
                  <span className="text-faint">{i + 1}.</span> {callLabel(c)}
                  <span className="ml-2 text-faint">{Math.round(c.ms)} ms</span>
                </p>
                {c.cypher.map((q, j) => (
                  <Cypher key={j} text={q} params={c.params[j]} />
                ))}
              </li>
            ))}
          </ol>
        </details>
      )}
    </article>
  );
}

type NewMessage = { role: "user"; text: string } | { role: "assistant"; result: AskResponse } | { role: "error"; text: string };

/** The conversation, kept in this tab across runs (a new incident does not clear it). */
export function useAskChat() {
  const [messages, setMessages] = useState<ChatMessage[]>(() => {
    if (typeof window === "undefined") return [];
    try {
      return readChat(window.sessionStorage.getItem(CHAT_KEY));
    } catch {
      return [];
    }
  });
  const [busy, setBusy] = useState(false);
  const nextId = useRef(messages.reduce((n, m) => Math.max(n, m.id + 1), 1));
  const inFlight = useRef(false);

  useEffect(() => {
    try {
      window.sessionStorage.setItem(CHAT_KEY, JSON.stringify(messages));
    } catch {
      /* storage full or blocked: the conversation lasts for this page only */
    }
  }, [messages]);

  const push = useCallback((m: NewMessage) => {
    const msg = { ...m, id: nextId.current++ } as ChatMessage;
    setMessages((all) => keepChat([...all, msg]));
  }, []);

  /** Send one question; returns why it was not sent (empty, too long, one in flight) or null. */
  const ask = useCallback(
    async (question: string, incidentId: string | null): Promise<string | null> => {
      const problem = checkQuestion(question);
      if (problem) return problem;
      if (inFlight.current) return "Wait for the current answer first.";
      inFlight.current = true;
      setBusy(true);
      push({ role: "user", text: question.trim() });
      try {
        const result = parseAsk(await askGraph(question.trim(), incidentId));
        if (result) push({ role: "assistant", result });
        else push({ role: "error", text: "The answer could not be read. Try asking again." });
      } catch (e) {
        if (e instanceof FallbackError) push({ role: "error", text: askErrorText(e.reason, "Too many questions. Try again later.") });
        else push({ role: "error", text: e instanceof Error ? e.message : "The request failed. Try asking again." });
      } finally {
        inFlight.current = false;
        setBusy(false);
      }
      return null;
    },
    [push],
  );

  const clear = useCallback(() => setMessages([]), []);
  return { messages, busy, ask, clear };
}

export type AskChat = ReturnType<typeof useAskChat>;

/** The button that opens the chat panel: in the console's bottom bar, or floating in the
 *  bottom-right corner where there is no bar (the welcome screen, the storm). */
export function AskLauncher({ onOpen, floating = false }: { onOpen: () => void; floating?: boolean }) {
  return (
    <button
      type="button"
      onClick={onOpen}
      aria-haspopup="dialog"
      className={`inline-flex items-center gap-2 rounded-full border border-ink bg-ink font-medium text-void hover:bg-ink/90 ${
        floating ? "fixed bottom-6 right-6 z-20 h-10 pl-3.5 pr-4 text-body shadow-lg shadow-black/30" : "h-7 pl-2.5 pr-3 text-label"
      }`}
    >
      <ChatIcon />
      Ask the graph
    </button>
  );
}

function ChatIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinejoin="round" aria-hidden>
      <path d="M2.5 3.5h11v7h-6l-3 2.5v-2.5h-2z" />
    </svg>
  );
}

/** "Ask the graph" as a chat: plain-English questions about the company in the session's
 *  world graph, and about the visitor's incident when one is open. Every answer cites its
 *  nodes and Cypher; asking never writes anything. A non-modal side panel, so source chips
 *  can still focus nodes on the graph behind it. */
export function AskPanel({
  chat,
  onClose,
  incidentId = null,
  rootCause = null,
  inGraph,
  onFocus,
}: {
  chat: AskChat;
  onClose: () => void;
  /** The live incident, sent as context; null for recordings and the welcome screen. */
  incidentId?: string | null;
  /** The blamed change, for the starter questions. */
  rootCause?: string | null;
  inGraph: (id: string) => boolean;
  onFocus: (ids: string[]) => void;
}) {
  const id = useId();
  const [question, setQuestion] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const field = useRef<HTMLTextAreaElement>(null);
  const end = useRef<HTMLDivElement>(null);
  const { messages, busy } = chat;

  useEffect(() => {
    field.current?.focus();
  }, []);
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);
  useEffect(() => {
    end.current?.scrollIntoView({ block: "end" });
  }, [messages.length, busy]);

  const send = async (q: string) => {
    const problem = await chat.ask(q, incidentId);
    if (problem) setErr(problem);
    else {
      setQuestion("");
      setErr(null);
    }
  };
  const submit = (e: FormEvent) => {
    e.preventDefault();
    void send(question);
  };
  const left = ASK_MAX - question.length;
  const starters = starterQuestions({ incidentId, rootCause });

  return (
    <aside
      role="dialog"
      aria-modal="false"
      aria-labelledby={`${id}-h`}
      className="fixed inset-y-0 right-0 z-30 flex w-full flex-col border-l border-rule-hi bg-deck shadow-2xl shadow-black/40 sm:w-[min(460px,100vw)]"
    >
      <header className="flex items-start gap-3 border-b border-rule px-5 py-3.5">
        <div className="min-w-0 flex-1">
          <h2 id={`${id}-h`} className="text-title font-semibold text-ink">Ask the graph</h2>
          <p className="mt-0.5 text-label text-dim">
            {incidentId
              ? `Ask about the company or about ${incidentId}. Answers cite the graph nodes and queries behind them.`
              : "Ask about the company's people, teams, services, changes and incidents. Answers cite the graph nodes and queries behind them."}
          </p>
        </div>
        {messages.length > 0 && (
          <Button size="sm" variant="ghost" onClick={chat.clear} disabled={busy}>
            Clear
          </Button>
        )}
        <button
          type="button"
          onClick={onClose}
          aria-label="Close the chat"
          title="Close (Esc)"
          className="-mr-1.5 inline-flex size-7 shrink-0 items-center justify-center rounded-ctl text-dim hover:bg-raise hover:text-ink"
        >
          <svg width="14" height="14" viewBox="0 0 14 14" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" aria-hidden>
            <path d="M3 3l8 8M11 3l-8 8" />
          </svg>
        </button>
      </header>

      <div className="min-h-0 flex-1 overflow-y-auto px-5 py-4" aria-live="polite" aria-busy={busy}>
        {messages.length === 0 && !busy ? (
          <div>
            <p className="text-body text-dim">
              Nothing you ask writes to the graph. Try one of these:
            </p>
            <ul className="mt-3 space-y-2">
              {starters.map((q) => (
                <li key={q}>
                  <button
                    type="button"
                    onClick={() => void send(q)}
                    className="w-full rounded-ctl border border-rule-hi bg-raise px-3 py-2 text-left text-body text-ink hover:border-dim"
                  >
                    {q}
                  </button>
                </li>
              ))}
            </ul>
          </div>
        ) : (
          <ol className="space-y-4">
            {messages.map((m) => (
              <li key={m.id} className={m.role === "user" ? "flex justify-end" : "flex justify-start"}>
                {m.role === "user" ? (
                  <p className="max-w-[85%] whitespace-pre-line rounded-lg rounded-br-sm bg-symptom/15 px-3.5 py-2 text-body text-ink">{m.text}</p>
                ) : m.role === "error" ? (
                  <p role="alert" className="max-w-[92%] rounded-lg rounded-bl-sm border border-drifted/50 bg-drifted/10 px-3.5 py-2 text-body text-ink">
                    {m.text}
                  </p>
                ) : (
                  <div className="w-full max-w-[96%]">
                    <AskAnswer result={m.result} inGraph={inGraph} onFocus={onFocus} />
                  </div>
                )}
              </li>
            ))}
            {busy && (
              <li className="flex justify-start">
                <p className="inline-flex items-center gap-2.5 rounded-lg rounded-bl-sm border border-rule bg-raise px-3.5 py-2.5 text-label text-dim">
                  <span className="flex gap-1" aria-hidden>
                    <span className="size-1.5 animate-bounce rounded-full bg-dim [animation-delay:-0.3s] motion-reduce:animate-none" />
                    <span className="size-1.5 animate-bounce rounded-full bg-dim [animation-delay:-0.15s] motion-reduce:animate-none" />
                    <span className="size-1.5 animate-bounce rounded-full bg-dim motion-reduce:animate-none" />
                  </span>
                  Reading the graph
                </p>
              </li>
            )}
          </ol>
        )}
        <div ref={end} />
      </div>

      <form onSubmit={submit} className="border-t border-rule px-5 pb-4 pt-3">
        <label htmlFor={id} className="sr-only">Question about the company</label>
        <div className="flex items-end gap-2">
          <textarea
            ref={field}
            id={id}
            rows={2}
            value={question}
            maxLength={ASK_MAX}
            placeholder={incidentId ? "Why did you blame this change? Who owns checkout?" : "Who owns checkout? What changed on search last week?"}
            onChange={(e) => {
              setQuestion(e.target.value);
              setErr(null);
            }}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
                e.preventDefault();
                void send(question);
              }
            }}
            className="min-h-[2.75rem] min-w-0 flex-1 resize-none rounded-ctl border border-rule-hi bg-void px-3 py-2 text-body text-ink placeholder:text-faint hover:border-dim"
          />
          <Button type="submit" variant="primary" disabled={busy || !question.trim()} className="!h-9 !px-3.5">
            {busy ? "Asking" : "Ask"}
          </Button>
        </div>
        <div className="mt-1.5 flex items-center gap-2 text-meta text-faint">
          <span>Enter to send, Shift+Enter for a new line</span>
          {left < 80 && <span className="ml-auto font-mono tabular-nums">{left} left</span>}
        </div>
        {err && <p role="alert" className="mt-1.5 text-label text-falsified">{err}</p>}
      </form>
    </aside>
  );
}
