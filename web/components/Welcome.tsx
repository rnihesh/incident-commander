"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { FAULT_CLASSES, getSessionIncidents, type FaultClass } from "../lib/api";
import { Button, buttonClass } from "./ui";

export const FAULT_COPY: Record<FaultClass, { label: string; hint: string }> = {
  bad_deploy: { label: "Bad deploy", hint: "A release breaks a service" },
  config_change: { label: "Config change", hint: "A setting change breaks a service" },
  flag_flip: { label: "Feature flag flip", hint: "A flag turns on a broken code path" },
};

const STEPS = [
  {
    title: "Break something",
    body: "Pick a kind of fault. It lands somewhere in your own private copy of the company graph, so nothing you do affects anyone else.",
  },
  {
    title: "The agent investigates",
    body: "It queries the graph live, rules out every change that cannot explain the symptoms, and names the one that does.",
  },
  {
    title: "Check the proof",
    body: "Every step is saved as a Cypher query you can re-run. In Judge mode you can tamper with the graph and try to make it wrong.",
  },
];

/** The console before anything has started: what this is, how it works, and one clear way in. */
export function Welcome({
  fault,
  onFault,
  onStart,
  starting,
  error,
  onSample,
  onStorm,
  storming,
}: {
  fault: FaultClass;
  onFault: (f: FaultClass) => void;
  onStart: () => void;
  starting: boolean;
  error: string | null;
  onSample: () => void;
  onStorm: () => void;
  storming: boolean;
}) {
  // A returning visitor's sandbox may already hold incidents: offer the latest one's replay.
  const [lastIncident, setLastIncident] = useState<string | null>(null);
  useEffect(() => {
    let cancelled = false;
    getSessionIncidents()
      // Storm ids have no replay page: only single incidents are offered.
      .then((ids) => !cancelled && setLastIncident(ids.filter((i) => i.startsWith("inc-")).at(-1) ?? null))
      .catch(() => { /* no session yet */ });
    return () => {
      cancelled = true;
    };
  }, []);

  return (
    <main className="min-h-0 overflow-y-auto">
      <div className="mx-auto grid min-h-full max-w-6xl content-center gap-x-14 gap-y-10 px-6 py-10 sm:px-10 lg:grid-cols-[minmax(0,1fr)_minmax(0,420px)] lg:py-14">
        <section aria-labelledby="welcome-h" className="max-w-[60ch]">
          <h1 id="welcome-h" className="text-headline font-semibold tracking-tight text-ink">
            An AI on-call engineer that shows its work
          </h1>
          <p className="mt-4 text-lead text-dim">
            Incident Commander runs a simulated company of 400 services across 3 regions as a graph in FalkorDB. Break
            something, and the agent finds the one change that caused the outage, proving each step
            with a query you can run yourself.
          </p>

          <form
            className="mt-8"
            onSubmit={(e) => {
              e.preventDefault();
              onStart();
            }}
          >
            <fieldset>
              <legend className="text-label font-medium text-ink">What should break?</legend>
              <div className="mt-2 grid gap-2 sm:grid-cols-3">
                {FAULT_CLASSES.map((f) => (
                  <label
                    key={f}
                    className={`cursor-pointer rounded-ctl border px-3 py-2.5 has-[:focus-visible]:outline-2 has-[:focus-visible]:outline-symptom ${
                      fault === f ? "border-symptom bg-symptom/10" : "border-rule-hi bg-deck hover:border-dim"
                    }`}
                  >
                    <input type="radio" name="fault" value={f} checked={fault === f} onChange={() => onFault(f)} className="sr-only" />
                    <span className="block text-body font-medium text-ink">{FAULT_COPY[f].label}</span>
                    <span className="block text-label text-dim">{FAULT_COPY[f].hint}</span>
                  </label>
                ))}
              </div>
            </fieldset>
            <div className="mt-5 flex flex-wrap items-center gap-x-5 gap-y-3">
              <Button type="submit" variant="primary" disabled={starting} className="!h-10 !px-5 !text-body">
                {starting ? "Starting the incident" : "Start an incident"}
              </Button>
              <Link href="/judge" className="text-body font-medium text-ink underline decoration-rule-hi underline-offset-4 hover:decoration-ink">
                Or try to fool it in Judge mode
              </Link>
            </div>
            {error && <p role="alert" className="mt-3 max-w-[60ch] text-label text-falsified">{error}</p>}
          </form>

          <div className="mt-6 flex flex-wrap items-center gap-2 text-label text-dim">
            <button type="button" onClick={onSample} className={buttonClass("ghost", "md", "-ml-2.5")}>
              Watch a sample run
            </button>
            <span className="text-faint">a saved recording, replayed</span>
          </div>
          <div className="flex flex-wrap items-center gap-2 text-label text-dim">
            <button type="button" onClick={onStorm} disabled={storming} className={buttonClass("ghost", "md", "-ml-2.5")}>
              {storming ? "Firing the storm" : "Fire an alert storm"}
            </button>
            <span className="text-faint">hundreds of pages from two or three faults at once</span>
          </div>
          {lastIncident && (
            <p className="mt-4 text-label text-dim">
              Your sandbox already has an incident.{" "}
              <Link href={`/replay/${encodeURIComponent(lastIncident)}`} className="font-medium text-ink underline decoration-rule-hi underline-offset-4 hover:decoration-ink">
                Open the replay of {lastIncident}
              </Link>
            </p>
          )}
        </section>

        <aside aria-label="How it works" className="lg:pt-2">
          <PathSketch />
          <h2 className="mt-6 text-body font-semibold text-ink">How it works</h2>
          <ol className="mt-3 space-y-4">
            {STEPS.map((s, i) => (
              <li key={s.title} className="grid grid-cols-[1.75rem_minmax(0,1fr)] gap-x-2">
                <span className="grid size-6 place-items-center rounded-full border border-rule-hi font-mono text-label text-dim" aria-hidden>
                  {i + 1}
                </span>
                <div>
                  <p className="text-body font-medium text-ink">{s.title}</p>
                  <p className="mt-0.5 text-label leading-relaxed text-dim">{s.body}</p>
                </div>
              </li>
            ))}
          </ol>
        </aside>
      </div>
    </main>
  );
}

/** The idea in one picture: the alerting service, the causal path back to the change that
 *  explains it, and two rival changes ruled out. Drawn with the theme tokens. */
function PathSketch() {
  return (
    <figure className="border border-rule bg-deck p-4">
      <svg viewBox="0 0 380 150" className="h-auto w-full" role="img" aria-labelledby="sketch-t">
        <title id="sketch-t">An alert traced back along its dependency path to the one change that explains it, with two other changes ruled out</title>
        <g className="stroke-rule-hi" strokeWidth="1.2" fill="none">
          <path d="M60 40 L150 75" />
          <path d="M60 112 L150 75" />
          <path d="M150 75 L245 40" />
          <path d="M245 112 L150 75" />
        </g>
        <g className="stroke-verified" strokeWidth="2.6" fill="none" strokeLinecap="round">
          <path d="M150 75 L245 40 L330 75" />
        </g>
        {/* the alerting service */}
        <circle cx="330" cy="75" r="9" className="fill-ink" />
        <circle cx="330" cy="75" r="15" className="stroke-falsified" strokeOpacity="0.5" fill="none" />
        {/* the path hops */}
        <circle cx="245" cy="40" r="6" className="fill-verified" />
        <circle cx="245" cy="112" r="5" className="fill-node" />
        {/* the root-cause change */}
        <circle cx="150" cy="75" r="7.5" className="fill-verified" />
        {/* rival changes, ruled out */}
        <g className="stroke-faint" strokeWidth="1.6">
          <circle cx="60" cy="40" r="6" className="fill-deck" />
          <path d="M55.5 35.5l9 9M64.5 35.5l-9 9" />
          <circle cx="60" cy="112" r="6" className="fill-deck" />
          <path d="M55.5 107.5l9 9M64.5 107.5l-9 9" />
        </g>
        <g className="font-mono" fontSize="10.5">
          <text x="330" y="104" textAnchor="middle" className="fill-dim">alert</text>
          <text x="140" y="106" textAnchor="middle" className="fill-verified">the change</text>
          <text x="60" y="20" textAnchor="middle" className="fill-faint">ruled out</text>
          <text x="60" y="140" textAnchor="middle" className="fill-faint">ruled out</text>
        </g>
      </svg>
      <figcaption className="mt-2 text-label text-dim">
        The agent rules out every other recent change until only one explains every symptom.
      </figcaption>
    </figure>
  );
}
