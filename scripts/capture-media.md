# Regenerating the README media

All files in `docs/media/` are captured from the running app (redesigned console, dark theme
unless noted) and assembled with ImageMagick and ffmpeg. Nothing is mocked: every console,
judge and Ask still comes from a live run against a local API. The benchmark stills render
the committed `api/bench_results/report.json` and `learning_curve.json`.

## Setup

- FalkorDB on `localhost:16379` with `seed:world` loaded.
- API on a spare port with the local `qwen3:8b` agent on Ollama, so no paid LLM is called and
  verdicts carry no DEGRADED badge. The relaxed limits allow repeated runs:

  ```sh
  cd api && IC_LLM_PROVIDER=ollama IC_LLM_MODEL=qwen3:8b IC_LLM_API_KEY= \
    IC_COOKIE_SECURE=false IC_WEB_ORIGIN=http://localhost:3014 \
    IC_IP_CONCURRENT_SESSIONS=8 IC_IP_SESSIONS_PER_HOUR=100 IC_IP_RUNS_PER_HOUR=100 \
    IC_SESSION_RUNS_PER_HOUR=100 IC_GLOBAL_RUNS_PER_HOUR=200 \
    uv run uvicorn ic.api.app:app --port 8014
  ```

  Without Ollama, leave `IC_LLM_PROVIDER` unset: the deterministic agent runs and every
  verdict shows the DEGRADED banner.
- Web: `cd web && NEXT_PUBLIC_API_URL=http://localhost:8014 pnpm dev -p 3014` (the dev
  indicator is hidden in `next.config`).
- Stills use the Chrome DevTools MCP tools in an isolated browser context. Set the viewport with
  emulation, not window resizing: **1440x900 at device pixel ratio 2** (raw shots are
  2880x1800), colour scheme dark, and `localStorage['ic.theme'] = 'dark'`.
- Raw screenshots go to `docs/media/.frames/` (the screenshot tool refuses paths outside the
  workspace). Delete that folder before committing.
- A qwen3:8b run takes about 30-40 s on a laptop; an Ask answer 5-20 s.
- Afterwards, delete the session graphs the runs created (`s:*`, see Checks) and close every
  page you opened.

## Graph framing

The canvas fits the graph when its size changes. After a run or a Verify, a focus animation can
leave it zoomed on a few nodes, and the storm graph can stay unfitted. Before a still, nudge the
viewport (emulate 1440x880, then 1440x900) to refit.

## Stills

| File | How |
|---|---|
| `welcome.png` | Fresh context, `/`. The welcome screen as first painted. |
| `console-verdict.png` | Welcome screen, **Start an incident** (Bad deploy), wait for the header badge "Resolved and remembered", screenshot: graph, funnel, verdict, certificate. Prefer a run with a multi-hop causal path. |
| `console-response.png` | Same run: scroll the **Response** heading to the top of the right column. |
| `xray.png` | Same run: expand the `origin` funnel cut, click **X-ray**, scroll "What we avoid / What we run" near the top. |
| `challenge.png` | Same run: close the X-ray, open the bottom tab **Was it my deploy?**, pick an eliminated change that fails `signature`, **Challenge**, open **Query that decided it**. Spends 1 of the incident's 2 challenges. |
| `ask.png` | Same run: **Ask the graph** (bottom right) opens the chat panel. Click the starter "Why did you blame chg-…?", then ask "Which changes caused incidents last week?", open the first answer's **Show queries**. |
| `console-light.png` | Same run: close the chat, scroll to the top, press the header sun icon (light theme), screenshot, switch back. |
| `proof-page.png` | Open the STATUS link (`/proof/<ref>`), click **Re-verify**. |
| `replay-scrubber.png` | `/replay/<incident id>`, set the range input to about 110 of 119. |
| `storm.png` | `/`, **Fire an alert storm**, wait for `done`, refit. |
| `judge-commit-reveal.png` | `/judge`, pick a service with many upstream services (e.g. elasticsearch-payments ap-south), **Commit and inject blind**, wait for LEARNED, **Reveal**, scroll **Commitment** to the top, refit. |
| `fragility.png` | Same round: **Find edits that flip the verdict**, scroll **Tamper** to the top of the right column. |
| `judge-tamper-falsified.png` | Same round: click the first "Try these" edit (pre-fills **Remove edge**), **Review edit**, **Confirm and write**, **Verify**; screenshot while the graph is zoomed on the responsible nodes. |
| `benchmark.png` | `/benchmark`, scroll "Live LLM runs" just below the sticky header. |
| `why-wrong.png` | Open the gpt-6-luna **Why it was wrong** and its first **deciding query**; scroll the summary near the top. |
| `learning-curve.png` | `/benchmark`, scroll "Learning curve" into view, crop the section to its content (the grid row stretches it). |

## GIFs

- `console-investigation.gif` (hero): a Playwright script (`playwright-core` with the system
  Chrome, headless, 1440x900 at DPR 1, `colorScheme: dark`) opens `/`, screenshots the welcome
  screen, hovers and clicks **Start an incident**, screenshots every ~0.7 s until the header
  shows "Resolved and remembered", then scrolls the **Response** heading into view. The frames
  are stitched with the ffmpeg concat demuxer: welcome 0.8 s per frame, run frames 0.25 s
  (about 3x time compression), verdict held 2.8 s, response held 3.6 s.
- `judge-tamper-falsified.gif`: the stills of the tamper sequence (Try these, edit pre-filled,
  review with the exact Cypher, written, FALSIFIED zoomed on the responsible nodes, refit, the
  hypothesis chain with FALSIFIED and DRIFTED), each held 1.6-3.2 s, scaled to 1440 wide.

Both use a per-GIF palette:

```sh
ffmpeg -y -f concat -safe 0 -i concat.txt \
  -vf "fps=8,split[s0][s1];[s0]palettegen=max_colors=192:stats_mode=diff[p];[s1][p]paletteuse=dither=bayer:bayer_scale=4:diff_mode=rectangle" \
  docs/media/console-investigation.gif   # under 1 MB
```

## Optimising stills

```sh
magick raw.png -strip -resize 2000x -colors 256 docs/media/out.png   # 70-330 KB each
```

## Checks

```sh
grep -o 'docs/media/[^")]*' README.md | sort -u | xargs ls -la   # every referenced file exists
rm -rf docs/media/.frames
redis-cli -p 16379 GRAPH.LIST            # then GRAPH.DELETE each s:* graph your runs created
```
