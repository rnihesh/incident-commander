"use client";

import { useEffect, useId, useMemo, useRef, useState } from "react";
import type { CertificateData, VerdictData } from "../lib/payloads";
import { RateLimitedError, challengeIncident, getChallengeBudget } from "../lib/api";
import { acceptFetchedBudget, challengeOptions, clauseLabel, parseChallenge, verdictLabel, type ChallengeResult } from "../lib/challenge";
import { Badge, Button, Cypher, Empty, PanelHead, Select, shortSvc } from "./ui";

const left = (remaining: number, limit: number) => `${remaining} of ${limit} challenge${limit === 1 ? "" : "s"} left`;

/** One challenge answer: verdict, the clause it fails in plain English, the deciding query. */
export function ChallengeResultView({ result }: { result: ChallengeResult }) {
  if (result.kind === "root") {
    return (
      <div className="arrive border border-rule-hi bg-raise p-3 text-label">
        <Badge tone="info">this is the diagnosed root</Badge>
        <p className="mt-2 text-ink/90">{result.message}. No challenge spent.</p>
        <p className="mt-1 font-mono text-meta text-dim">{left(result.remaining, result.limit)}</p>
      </div>
    );
  }
  const refuted = result.verdict === "REFUTED";
  const plain = clauseLabel(result.clause);
  return (
    <div className={`arrive space-y-2 border p-3 text-label ${refuted ? "border-verified/50 bg-verified/5" : "border-falsified/50 bg-falsified/5"}`}>
      {result.wouldOverturn && (
        <div role="status" className="border border-falsified/70 bg-falsified/10 px-2 py-1.5 font-mono text-label text-falsified">
          would overturn: INCONCLUSIVE
          <span className="ml-2 font-sans text-meta text-ink/80">
            This change qualifies on the current world. The recorded verdict stands until you re-derive.
          </span>
        </div>
      )}
      <div className="flex flex-wrap items-center gap-2">
        <Badge tone={refuted ? "verified" : "falsified"}>{verdictLabel(result)}</Badge>
        <span className="font-mono text-ink">{result.change}</span>
        {result.service && <span className="font-mono text-dim">on {shortSvc(result.service)}</span>}
      </div>
      {refuted ? (
        <p className="text-ink/90">{plain ?? "It fails the root-cause predicate."}</p>
      ) : (
        <p className="text-ink/90">
          It passes every clause{result.rivals.length ? `, alongside ${result.rivals.join(", ")}` : ""}.
        </p>
      )}
      {result.detail && <p className="font-mono text-meta text-dim">{result.detail}</p>}
      {result.queryText && (
        <details>
          <summary className="cursor-pointer font-mono text-meta text-dim hover:text-ink">Query that decided it</summary>
          <div className="mt-1"><Cypher text={result.queryText} /></div>
        </details>
      )}
      <p className="font-mono text-meta text-dim">{left(result.remaining, result.limit)}</p>
    </div>
  );
}

/** "Was it my deploy?": pick a change the investigation eliminated and re-check it live with
 * the agent's own predicate on a fresh as-of build of the current world. */
export function Challenge({
  cert,
  verdict,
  incidentId = null,
}: {
  cert: CertificateData | null;
  verdict: VerdictData | null;
  /** The live incident; null for recorded runs and mock scenarios. */
  incidentId?: string | null;
}) {
  const id = useId();
  const [pick, setPick] = useState("");
  const [answer, setAnswer] = useState<{ incident: string; result: ChallengeResult } | null>(null);
  const [budget, setBudget] = useState<{ incident: string; remaining: number; limit: number } | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  // The incident ask() last wrote a budget for; a slower initial GET must not overwrite that.
  const askedRef = useRef<string | null>(null);
  const options = useMemo(() => challengeOptions(cert, verdict), [cert, verdict]);

  useEffect(() => {
    if (!incidentId) return;
    let live = true;
    getChallengeBudget(incidentId)
      .then((b) => live && acceptFetchedBudget(askedRef.current, incidentId) && setBudget({ incident: incidentId, remaining: b.remaining, limit: b.limit }))
      .catch(() => {});
    return () => {
      live = false;
    };
  }, [incidentId]);

  if (!incidentId) {
    return (
      <section aria-labelledby="chal-h">
        <PanelHead id="chal-h" title="Was it my deploy?" />
        <Empty>
          Challenges re-check a change live, so they need a live incident. Inject a fault and, once the verdict is in, pick
          any change it eliminated to see which clause rules it out on the current world. Two challenges per incident.
        </Empty>
      </section>
    );
  }

  const shown = budget?.incident === incidentId ? budget : null;
  const result = answer?.incident === incidentId ? answer.result : null;
  const remaining = shown?.remaining ?? null;
  const limit = shown?.limit ?? 2;
  const spent = remaining === 0;

  const ask = async () => {
    if (!pick) return;
    setBusy(true);
    setErr(null);
    try {
      const parsed = parseChallenge(await challengeIncident(incidentId, pick));
      if (parsed) {
        setAnswer({ incident: incidentId, result: parsed });
        askedRef.current = incidentId;
        setBudget({ incident: incidentId, remaining: parsed.remaining, limit: parsed.limit });
      } else setErr("The backend answer was not recognised.");
    } catch (e) {
      if (e instanceof RateLimitedError) {
        askedRef.current = incidentId;
        setBudget({ incident: incidentId, remaining: 0, limit });
        setErr(e.reason ?? "Both challenges for this incident are used.");
      } else setErr(e instanceof Error ? e.message : "request failed");
    } finally {
      setBusy(false);
    }
  };

  return (
    <section aria-labelledby="chal-h">
      <PanelHead id="chal-h" title="Was it my deploy?" meta={remaining === null ? undefined : left(remaining, limit)} />
      {!options.length ? (
        <Empty>Available once the verdict is in. Pick any change the investigation eliminated and challenge it live.</Empty>
      ) : (
        <div className="max-w-3xl space-y-2 px-inset py-3">
          <div className="flex items-center gap-2">
            <label htmlFor={id} className="sr-only">Change to challenge</label>
            <Select
              id={id}
              value={pick}
              onChange={(e) => { setPick(e.target.value); setErr(null); }}
              className="min-w-0 flex-1"
            >
              <option value="">Choose an eliminated change</option>
              {options.map((o) => (
                <option key={o.change} value={o.change}>
                  {o.change} on {shortSvc(o.service)}{o.root ? " (diagnosed root)" : ""}
                </option>
              ))}
            </Select>
            <Button
              onClick={() => void ask()}
              disabled={!pick || busy || (spent && !options.find((o) => o.change === pick)?.root)}
            >
              {busy ? "Checking" : "Challenge"}
            </Button>
          </div>
          {err && <p role="alert" className="text-label text-falsified">{err}</p>}
          {result && <ChallengeResultView result={result} />}
        </div>
      )}
    </section>
  );
}
