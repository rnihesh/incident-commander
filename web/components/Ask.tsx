"use client";

import { useId, useRef, useState, type FormEvent } from "react";
import { RateLimitedError, askGraph } from "../lib/api";
import {
  ASK_MAX,
  EXAMPLE_QUESTIONS,
  MODE_LABEL,
  STATUS_LABEL,
  answerMeta,
  askStatus,
  callLabel,
  checkQuestion,
  parseAsk,
  type AskResponse,
  type AskStatus,
} from "../lib/ask";
import { Badge, Button, Chip, Cypher, Input, PanelHead, type Tone } from "./ui";

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

/** "Ask the graph": plain-English questions about the company in the session's world graph,
 *  answered with sources. Single turn; asking never writes anything. */
export function Ask({
  incidentId = null,
  inGraph,
  onFocus,
}: {
  /** The live incident, sent as context; null for recorded runs. */
  incidentId?: string | null;
  inGraph: (id: string) => boolean;
  onFocus: (ids: string[]) => void;
}) {
  const id = useId();
  const [question, setQuestion] = useState("");
  const [answer, setAnswer] = useState<{ question: string; result: AskResponse } | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const seq = useRef(0);

  const ask = async (q: string) => {
    const problem = checkQuestion(q);
    if (problem) {
      setErr(problem);
      return;
    }
    const n = ++seq.current;
    setBusy(true);
    setErr(null);
    try {
      const result = parseAsk(await askGraph(q.trim(), incidentId));
      if (n !== seq.current) return; // a newer question superseded this one
      if (result) setAnswer({ question: q.trim(), result });
      else setErr("The backend answer was not recognised.");
    } catch (e) {
      if (n !== seq.current) return;
      if (e instanceof RateLimitedError) setErr(e.reason ? `${e.reason[0].toUpperCase()}${e.reason.slice(1)}. Try again later.` : "Too many questions. Try again later.");
      else setErr(e instanceof Error ? e.message : "request failed");
    } finally {
      if (n === seq.current) setBusy(false);
    }
  };

  const submit = (e: FormEvent) => {
    e.preventDefault();
    void ask(question);
  };
  const left = ASK_MAX - question.length;

  return (
    <section aria-labelledby="ask-h">
      <PanelHead id="ask-h" title="Ask the graph" meta="people, teams, services, changes, incidents, decisions" />
      <div className="max-w-4xl space-y-2.5 px-inset py-3">
        <form onSubmit={submit} className="flex items-center gap-2">
          <label htmlFor={id} className="sr-only">Question about the company</label>
          <Input
            id={id}
            mono={false}
            value={question}
            maxLength={ASK_MAX}
            placeholder="Who owns checkout? What changed on search last week?"
            onChange={(e) => {
              setQuestion(e.target.value);
              setErr(null);
            }}
            className="min-w-0 flex-1"
            autoComplete="off"
          />
          <Button type="submit" variant="primary" disabled={busy || !question.trim()}>
            {busy ? "Asking" : "Ask"}
          </Button>
        </form>
        <div className="flex flex-wrap items-center gap-1.5">
          {EXAMPLE_QUESTIONS.map((q) => (
            <Chip
              key={q}
              mono={false}
              active={answer?.question === q}
              onClick={busy ? undefined : () => {
                setQuestion(q);
                void ask(q);
              }}
            >
              {q}
            </Chip>
          ))}
          {left < 80 && <span className="ml-auto font-mono text-meta tabular-nums text-faint">{left} left</span>}
        </div>
        {err && <p role="alert" className="text-label text-falsified">{err}</p>}
        <div aria-live="polite" aria-busy={busy}>
          {busy && (
            <p className="flex items-center gap-2 text-label text-dim">
              <span className="inline-block h-1.5 w-1.5 animate-pulse rounded-full bg-symptom" aria-hidden />
              Reading the graph
            </p>
          )}
          {!busy && answer && <AskAnswer result={answer.result} inGraph={inGraph} onFocus={onFocus} />}
          {!busy && !answer && !err && (
            <p className="text-label text-faint">
              Every answer lists the graph nodes it relied on and the Cypher that found them. Nothing you ask writes to the graph.
            </p>
          )}
        </div>
      </div>
    </section>
  );
}
