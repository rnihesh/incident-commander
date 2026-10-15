# Video script: Incident Commander (120 s)

Target: 2:00 exactly. One narrator, screen capture only. Every number spoken below comes from
`api/bench_results/report.md` or from a run on seed 42. Do not round them differently on
camera.

## Recording settings

- **Resolution.** 1920×1080, 30 fps. Chrome in a clean profile with no extensions and the
  window maximized. Hide the bookmarks bar.
- **Zoom.** 110% for the console and judge pages (the console type is 11-14 px), 125% for
  `/benchmark`, and 100% for the architecture card.
- **World.** Run `uv run ic seed --seed 42`. The content hash must print
  `879505a4501940838d15ac0743f6891686655da34e39d24e7521e99b577352ae`.
- **Agent.** `api/.env` holds `IC_LLM_PROVIDER=openai`, `IC_LLM_MODEL=gpt-6-luna` and the key, so
  the LLM triage agent runs (about $0.003 per incident; the breaker caps the day at $0.50).
  Verdicts have no DEGRADED badge. Without a key the deterministic graph agent runs and
  verdicts show DEGRADED; then say "deterministic graph agent" once on camera.
- **First paint.** A fresh browser profile (no `ic.started` in sessionStorage, no session
  incident) opening `/` plays the recorded hero run on its own, with its saved incident graph.
  That is beat 1; no click is needed. X-ray and the judge challenge need a live incident, so
  beats 2d and 3a use config B.
- **Two API configurations.** Restart the API between them.
  - **A (recorded hero, scrubbable).**
    `IC_COOKIE_SECURE=false IC_SESSION_RUNS_PER_HOUR=0 uv run uvicorn ic.api.app:app --port 8000`.
    With the run limit at 0, **Inject fault** plays the saved real run
    (`api/src/ic/runs/recorded/hero.json`, regenerate with `uv run ic record-hero`). It comes
    with the playback bar (0.5x-8x and a scrubber) and the banner "Recorded run". That banner is
    the replay disclosure, so keep it visible in at least one frame.
  - **B (live, judge beats).**
    `IC_COOKIE_SECURE=false IC_IP_CONCURRENT_SESSIONS=8 IC_IP_SESSIONS_PER_HOUR=100 IC_IP_RUNS_PER_HOUR=100 IC_SESSION_RUNS_PER_HOUR=100 IC_GLOBAL_RUNS_PER_HOUR=200 uv run uvicorn ic.api.app:app --port 8000`.
- **Do not** use `?scenario=` fixtures for the hero, judge or benchmark beats. They are
  hand-built UI fixtures with placeholder hashes. If one is used anyway (for example
  `?scenario=refused` for a cutaway), label it "UI fixture" on screen.
- **Live runs are fast** (about 1.2 s end to end). Capture at full speed and slow the footage
  in the edit, or pause on panels. Do not fake timing on screen. Put "time-compressed" or
  "slowed 4x" captions where applicable.
- **Audio.** Narration recorded separately at -16 LUFS, no music under the judge beat.

## Beat table

| # | Time | On screen | Narration (at most 2 lines) | Launch |
|---|---|---|---|---|
| 1 | 0:00-0:08 | Black, then a pager notification card: "Request timeouts on exports-batch" (the recorded run's title). Cut to a fresh profile opening the console: the recorded run is already playing, "Recorded run" banner in frame. | "At 3 AM an alert names a symptom, not a cause. Most AI SRE tools give you a ranked guess. This one gives you proofs you can re-run, or break." | Fresh profile, `http://localhost:3001/` (autoplay) |
| 2a | 0:08-0:14 | The reflex panel fills: search space, symptomatic count, top suspects, on-call, about 2 ms. | "Within milliseconds, before any model call: blast radius, suspects, who's on call. Labelled pre-snapshot." | Same page, playback 0.5x |
| 2b | 0:14-0:24 | The elimination funnel cuts, then verdict DIAGNOSED. The absence certificate draws its clause bars (one five-segment bar per rival, first failure in amber) over the onset strip. Hover one bar. | "FalkorDB copies the world as it stood at alert time. Every change is cut clause by clause, and each rival shows exactly which clause it failed." | Scrub as needed |
| 2c | 0:24-0:32 | Hypothesis chain with prev/hash, then the response panel: REHEARSAL CLEARED, PR, STATUS, PAGE, MEMORY, LEARNED. Caption: "simulated: no GitHub token in this take" unless a sandbox token is set. | "Each hypothesis is hash-chained before it runs. The rollback is rehearsed on a cloned world, then a PR, a page, and memory written back." | |
| 2d | 0:32-0:42 | Live run (config B, **Inject fault**). Expand the `origin` cut, click **X-ray**: the operator tree with records and ms, total time. | "Every cut is a real FalkorDB query. X-ray shows its plan and a live profile, operator by operator." | API config B, `http://localhost:3001/` |
| 3a | 0:42-0:50 | Drawer **Was it my deploy?**: pick an eliminated change, **Challenge**. REFUTED with the clause ("config cannot produce latency") and **Query that decided it**; "1 of 2 challenges left". | "Think it was your deploy? Challenge it. The same rule re-checks it live and tells you which clause rules it out." | Same page |
| 3b | 0:50-1:00 | `/judge`: search a service, pick a fault class, **Commit and inject blind**. The commitment hash appears; the run streams. **Reveal** shows the recomputed hash and MATCH. | "Don't trust us. Judge mode: you pick the target, we publish only a hash, the agent investigates blind, and reveal checks it." | `http://localhost:3001/judge` |
| 3c | 1:00-1:11 | **Find edits that flip the verdict**: "Try these" lists the single edits and the clause each breaks. Click one, **Review edit**, **Confirm and write** (Cypher shown), **Verify**: FALSIFIED, the responsible node named and highlighted. | "We even tell you how to break it: every single edit that would flip the verdict, each tried on its own copy. Apply one, and Verify turns the proof FALSIFIED." | Same page |
| 4 | 1:11-1:23 | Console "Learning curve" panel (or `/benchmark`): bars 9, 5, 5, 5, 5, 5, 5, 5, 9; the confidence line rises to 0.99, halves twice (÷2) through the dashed 0.25 threshold; incident 9 is back to 9. | "Resolved incidents teach the graph a coupling no one declared: five tool calls, not nine. When the world changes, contradictions halve that memory until it is dropped." | `http://localhost:3001/` or `/benchmark` |
| 4b | 1:23-1:33 | Console drawer, **Ask the graph** tab. Click the example "Who owns checkout and who is on call?" (or type "Who owns checkout and who is on call right now?"): answer with the LLM and grounded badges, source chips; expand **Show queries**. Quick second question: "Which changes caused incidents last week?". Then an off-graph question, "What is the weather in Paris today?": the answer reads "The graph has no information about the weather in Paris today." with a "no information" badge and 0 queries. | "Same graph, plain English. Ask who owns checkout and who is on call: every claim cites its nodes and the Cypher behind it. Ask something the graph does not hold, and it says so instead of guessing." | Drawer, Ask the graph (config B or a key set; about $0.001 per question) |
| 5 | 1:33-1:50 | `/benchmark`: live LLM table (gpt-6-luna behind every approach: graph 50/50, full context 26/50, vector RAG 4/50). Open **Why it was wrong**: chips "signature 19", "origin 4", "after onset 1", "no claim 41"; open one deciding query. Then the scale table (closure 3.15 vs 5.62 ms, path 1.01 vs 10.16 ms). Caption: "same model in every row". | "Fifty blind faults: only the graph agent gets all fifty, and every wrong answer is refuted by the same rule. At eight thousand services FalkorDB is two to ten times faster." | `http://localhost:3001/benchmark` |
| 6 | 1:50-2:00 | Architecture card (see below), then the repo URL. | "One FalkorDB instance, a graph per session, snapshots by GRAPH.COPY, and every claim a query you can run. Incident Commander." | Static card |

Beat totals: 8 + 34 + 29 + 12 + 10 + 17 + 10 = 120 s.

## Architecture card (beat 6)

A single 1920×1080 slide on a dark background, built from `docs/architecture.md`:

- **Left.** Browser, then FastAPI. Next to FastAPI: triage agent, ledger graph, remediation
  agent.
- **Centre.** FalkorDB with the graph names stacked: `seed:world`, `s:{sid}:world`,
  `s:{sid}:ledger`, `asof:{iid}:true|known`, `rehearse:{iid}`, `storm:{id}`.
- **Right.** GitHub PR + status, Discord page, MCP clients.
- **Bottom strip** (monospace): `GRAPH.COPY · IMPACTS · shortestPath · algo.BFS · algo.SPpaths · algo.WCC · algo.betweenness(samplingSeed) · vector + full-text indexes`.

## Shot list

| Shot | Beat | Capture | Notes |
|---|---|---|---|
| S1 | 1 | Pager card (static image), then a fresh-profile capture of `/` | Use the recorded run's title, "Request timeouts on exports-batch" (check `run_started.title` in `hero.json` if it was re-recorded). Do not invent a service name. Keep the "Recorded run" banner in frame for at least 1 s. |
| S2 | 2a-2c | Same take, playback 0.5x | Pause on the funnel, the clause bars and the response panel. |
| S3 | 2d | Screen capture, config B, `/`, a live **Inject fault** | Expand the `origin` cut, then **X-ray**. Crop so the records and ms columns stay readable at 1080p. |
| S4 | 3a | Same take: drawer **Was it my deploy?** | Pick a change that fails `signature` or `origin`. Each incident allows two challenges. |
| S5 | 3b | Screen capture, config B, `/judge` | Pick a service with an alert path. Wait for the verdict before **Reveal** (otherwise it opens after 120 s). |
| S6 | 3c | Same take as S5: Try these, Tamper, Verify | Keep "Recorded before the alert" ticked so the flip is not marked late-arriving (simpler to narrate). |
| S7 | 3c (optional) | FalkorDB Browser, `localhost:13000`, select `s:<ic_sid>:world` | Follow `docs/notes/browser-tamper.md`. Local only. Show the Cypher editor for 2 s. |
| S8 | 4 | Learning curve panel | Static. Slow push-in on the ÷2 marks and the 0.25 line. |
| S8b | 4b | Console drawer, Ask the graph, three questions | Questions 1 and 2 are the drawer's example chips; question 3 ("What is the weather in Paris today?") is off-graph and must show "no information". Hold the Show queries panel for 2 s. Needs the LLM key, otherwise the badge reads limited mode. |
| S9 | 5 | `/benchmark` at 125% zoom: live LLM table, Why it was wrong, scale table | Hold each 6-8 s. Highlight the rows being spoken. |
| S10 | 6 | Architecture card | Static. |

## Pre-flight checklist

- [ ] `docker compose --profile dev up -d`. `curl localhost:8000/health` returns `"falkordb": true`.
- [ ] Seed hash matches (above).
- [ ] A fresh profile on `/` autoplays the recorded run ("Recorded run" banner, playback bar).
- [ ] Config A: Inject fault shows "Recorded run" and the playback bar.
- [ ] Config B: a console run shows **X-ray** on its funnel cuts and the drawer challenge
      answers REFUTED for an eliminated change.
- [ ] Config B: a judge round ends DIAGNOSED, Reveal shows `commitment_ok: true` and a match,
      "Try these" lists at least one edit, and tamper plus Verify shows FALSIFIED.
- [ ] The drawer's **Ask the graph** answers "Who owns checkout and who is on call?" with LLM and grounded badges, and the Paris question with "no information".
- [ ] `/benchmark` shows 50 cases, the scale table, the learning curve with its ÷2 marks, and
      "Why it was wrong" for gpt-6-luna and claude-opus-5-5.
- [ ] No API keys or tokens visible anywhere in frame (terminal, DevTools, `.env`).
- [ ] The video description says which agent ran (gpt-6-luna, or deterministic if no key),
      whether the hero beat is replayed from a recorded run, and that PR and page are
      simulated unless a sandbox token was configured.
