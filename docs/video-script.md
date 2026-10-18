# Video script: Incident Commander (2:50)

A 2:30-3:00 YouTube demo of the redesigned console, recorded on the live site
([commander.niheshr.com](https://commander.niheshr.com)). One narrator, screen capture only.
Every click below was checked against the current UI. Change ids, service names and timings
differ from run to run: read them off the screen, do not script them.

Benchmark numbers come from `api/bench_results/` (the `/benchmark` "Live LLM runs" table):
with `gpt-6-luna` behind every approach on 50 blind faults, the graph agent scored **50/50**,
full context in the prompt **26/50** and vector RAG **4/50**. Say them exactly so.

## What the live site runs

- **Agent.** The live site runs **Claude Haiku** (`claude-haiku-5-5`). A live investigation
  takes a few seconds end to end. Do not speed it up on screen; if you trim dead time in the
  edit, add a small "trimmed" caption.
- **Limits.** Each sandbox allows 3 live runs per hour (a Judge round counts as a run), 6 per
  IP, and 20 Ask questions per hour. This plan uses 2 runs (one console incident, one Judge
  round), which leaves one retake. Past a limit the page shows a plain message such as
  "Hourly limit reached"; see the fallback plan.
- **Simulated actions.** Without GitHub and Discord tokens the PR, commit status and page are
  marked "(simulated)". Keep that word visible; do not crop it out.
- **Navigation resets state.** Leaving the console or Judge page discards the run on screen
  (the console reopens on the welcome screen). Finish each section before switching pages, or
  open the next page in a new tab.

## Shot list

| # | Time | Clicks | On screen | Voice-over |
|---|---|---|---|---|
| 1 | 0:00-0:10 | None. Title card over the welcome screen. | Card: "50 blind faults, same model behind every approach. Graph agent 50/50 · full context 26/50 · vector RAG 4/50." | "AI on-call tools guess. Give the same model fifty planted outages: with a FalkorDB graph it finds all fifty root causes. Full context finds 26, vector RAG finds 4." |
| 2a | 0:10-0:18 | `/`. Leave **Bad deploy** selected under "What should break?". Click **Start an incident**. | Welcome screen: headline, the three fault cards, "How it works", then the console opening with the header badge "Investigating". | "This is Incident Commander. I break something in my own private copy of a company's service graph, and an AI on-call agent investigates." |
| 2b | 0:18-0:30 | None; let it stream. Hover one dimmed node on the graph. | "First guess" strip (suspects, on-call, in a few ms), then the **Elimination funnel** rows cutting: `after_onset`, `origin`, `coverage`, `order`, `signature`. The graph lights the causal path in green. | "First it rebuilds the graph as it stood at alert time. Then every recent change is cut, rule by rule, until only a change that explains every symptom is left." |
| 2c | 0:30-0:40 | None. Point at the right column. | **Verdict**: Root cause `chg-…`, who deployed it. **Absence certificate**: "No rival qualifies", one clause bar per rival with its first failing clause. | "The verdict names the change. And the absence certificate proves no other change fits: each rival, and the exact rule it failed." |
| 2d | 0:40-0:50 | Scroll the right column to **Response**. | HANDOFF, **REHEARSAL CLEARED** (cloned world, candidates re-checked), PR, STATUS, PAGE, MEMORY; header badge "Resolved and remembered". | "Before acting, it rehearses the rollback on a cloned graph. Only then does it open the rollback PR, page the on-call owner, and write what it learned back into the graph." |
| 3 | 0:50-1:08 | Scroll up to **Hypothesis chain**. On proof 1 click **Show query**, then **Re-run** (badge VERIFIED). Click **Verify chain** (every proof VERIFIED). | The stored Cypher with its parameters, the prev/hash pair, VERIFIED badges, "chain head … anchored at completion". | "Every claim was written down before it was tested, and each proof is a stored Cypher query in a hash chain. Show the query, re-run it, or verify the whole chain yourself." |
| 4 | 1:08-1:25 | Click **Ask the graph** (bottom right). Click the starter **Why did you blame chg-…?**. Then open **Show queries** on the answer. | Chat panel: the question, the answer with "LLM" and "grounded" badges, source chips (Incident, Change), the Cypher behind it. | "I can just ask it. Why did you blame this change? The answer is grounded: every source it cites was touched by a query, and here are the queries." |
| 5a | 1:25-1:40 | Close the chat (×). Header **Judge mode**. Type `elasticsearch-payments` in the search box, click a service marked ALERT PATH OK (e.g. `ap-south`), keep Fault class **bad deploy**, click **Commit and inject blind**. | "Pick a target" list and its detail (upstream services, recent changes); then the commitment hash and the blind run streaming. | "Don't trust me. In Judge mode you plant the fault. The server publishes only a hash of your choice, and the agent investigates blind." |
| 5b | 1:40-1:50 | Wait for "VERDICT DIAGNOSED". Click **Reveal**. | Committed fault next to the agent's blind verdict, **MATCH**, "Recomputed hash = committed hash (also recomputed in this browser)". | "Reveal: the hash matches what was committed, and the agent named the exact change I planted." |
| 5c | 1:50-2:05 | In **Tamper**, click **Find edits that flip the verdict**. Click the first "Try these" edit (it pre-fills **Remove edge**). **Review edit**, **Confirm and write**, then **Verify**. | "Try these": every single edit that would flip the verdict, each tried on its own copy. The exact Cypher of the write, CHANGED, then **FALSIFIED** with the responsible nodes ringed red on the graph. | "Now try to break it. It even lists the edits that would. I remove one dependency on the causal path, verify, and the proof flips to FALSIFIED, pointing at what changed." |
| 6 | 2:05-2:18 | Same page. Click the `origin` row of the **Elimination funnel**, then **X-ray**. Scroll to "What we avoid / What we run". | FalkorDB's EXPLAIN plan of the naive valid-time query (Filter above the variable-length traverse, highlighted) beside the live PROFILE of the real query on the as-of snapshot, records and ms per operator. | "Every cut is a real FalkorDB query, and X-ray shows FalkorDB's own plan and profile. It also shows why we snapshot the graph instead of filtering time inside the traversal." |
| 7 | 2:18-2:33 | Header **Benchmark**. Scroll to **Live LLM runs**. | `gpt-6-luna` rows: graph 100% (50/50), full_context 52% (26/50), vector_rag 8% (4/50); `qwen3:8b` graph 50/50. Optionally open **Why it was wrong**. | "Same model, fifty blind faults. Only the graph agent gets all fifty, and a small local model driving the same graph tools does too." |
| 8 | 2:33-2:50 | Static closing card. | FalkorDB features used (list below) and the two links. | "One FalkorDB instance: a graph per sandbox, snapshots with GRAPH.COPY, graph algorithms for the investigation, and every claim a query you can run. Try it at commander.niheshr.com." |

Section totals: hook 10 s, investigation 40 s, proof 18 s, Ask 17 s, Judge 40 s, X-ray 13 s,
benchmark 15 s, close 17 s = 2:50.

### Closing card (shot 8)

A single 1920x1080 slide in the dark theme:

- **Title:** Incident Commander, an on-call agent whose every answer is a proof.
- **FalkorDB features used:** `GRAPH.COPY` (a world per sandbox, as-of snapshots, rollback
  rehearsal, PR world copies) · bitemporal pruning to an as-of graph · `shortestPath` /
  `allShortestPaths` over `IMPACTS` · `algo.BFS` · `algo.SPpaths` · `algo.WCC` (storm
  collapse) · `algo.betweenness` with a seed (chokepoints) · vector and full-text indexes
  (precedents) · `GRAPH.RO_QUERY` for every tool read · `GRAPH.EXPLAIN` / `GRAPH.PROFILE`
  (X-ray) · conditional `SET` as the agents' only coordination.
- **Links:** commander.niheshr.com · github.com/rnihesh/incident-commander

## If short on time

Cut in this order until the video fits; the hook, shots 2a-2d, 5a-5c and the close stay:

1. Shot 6 (X-ray), or shorten it to the plan table only (save 8-13 s).
2. Shot 3: keep **Re-run** on one proof, drop **Verify chain** (save 8 s).
3. Shot 4: show the answer, skip **Show queries** (save 5 s).
4. Shot 7: one 6 s hold on the gpt-6-luna rows, no "Why it was wrong".

Optional extras if there is time to spare (each about 8 s): **Fire an alert storm** from the
welcome screen (hundreds of pages collapse into 2-3 incidents, one jointly caused); the bottom
tab **Was it my deploy?** (pick an eliminated change, **Challenge**, REFUTED with the clause);
the header **Replay** link (scrub the timeline, as-happened / as-known); an off-graph Ask
question such as "What is the weather in Paris today?" (answered "no information").

## Recording tips

- **Viewport 1440x900.** The layout is designed for it. Use a fresh Chrome profile with no
  extensions, size the window so the page area is 1440x900 (for example
  `open -na "Google Chrome" --args --user-data-dir=/tmp/ic-rec --window-size=1440,1010`, then
  check `innerWidth` / `innerHeight` in the console), hide the bookmarks bar (Cmd+Shift+B),
  and close DevTools before recording.
- **Browser zoom.** 100% on the console and Judge pages (higher zoom wraps the right column
  and pushes the verdict below the fold). 125% is fine on `/benchmark` for the table shot.
- **Resolution.** Record on a Retina display (2x) and export 1080p or 1440p at 30 fps; small
  monospace text (hashes, Cypher) stays readable.
- **Theme.** Record in the **dark** theme: the green causal path, the red FALSIFIED rings and
  the clause bars have the most contrast, and it matches the README images. The header sun /
  moon toggle switches themes; a 2 s light-theme cutaway is optional.
- **Clean frame.** Do Not Disturb on, no other tabs visible, no API keys or terminal windows in
  frame, a cursor highlighter on for the click beats. Do not use `?scenario=` URLs; those
  are UI fixtures, not runs.
- **Audio.** Narration recorded separately at -16 LUFS; no music under the Judge section.

## Fallback if a live run is slow or refused

- If a run has not reached the verdict within about 20 s, reload the page (Cmd+R) to get the
  welcome screen back and press **Watch a sample run**, or open
  `commander.niheshr.com/?sample` directly. If the server refuses a live run at a limit, the
  console switches to the same recording on its own and a plain notice (for example "Hourly
  limit reached" or "The demo is busy") says why. The sample replays a saved real
  investigation with the banner "Sample run (recording)" and a playback bar (**Pause**,
  0.5x-8x, a scrubber), so shots 2a-2d can be paced exactly. Keep the banner in frame for at
  least a second and say "a saved run" once.
- A recording has no live incident behind it, so **Re-run**, **Verify chain**, **X-ray** and
  "Was it my deploy?" are not available there. Take shot 3 and shot 6 from the Judge round
  instead (its proofs, funnel and X-ray are live), or from a local take.
- Local take: run the stack with relaxed limits (see `scripts/capture-media.md`). With a local
  `qwen3:8b` on Ollama a run takes about 30-40 s, so trim it in the edit and caption the shot
  "local model, trimmed". Say which agent ran in the video description.

## Pre-flight checklist

- [ ] `https://commander.niheshr.com` opens on the welcome screen in a fresh profile.
- [ ] A test run on a second profile ends with "Resolved and remembered" and no "Degraded"
      banner (Haiku is answering), then that profile is closed so it does not use your runs.
- [ ] The **Ask the graph** button is bottom right and the starter questions include "Why did
      you blame …?" once an incident exists.
- [ ] `/judge` lists services with "ALERT PATH OK" and **Commit and inject blind** is enabled
      after picking one.
- [ ] `/benchmark` shows the "Live LLM runs" table with gpt-6-luna 50/50, 26/50, 4/50.
- [ ] The closing card and the title card are rendered at 1920x1080.
- [ ] The video description says the agent is Claude Haiku on the live site, that PR, status
      and page are simulated, and whether any shot is the sample recording.
