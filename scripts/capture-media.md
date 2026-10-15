# Regenerating the README media

All files in `docs/media/` were captured from the running app with the Chrome DevTools MCP
tools (any headless Chrome driver works) and assembled with ImageMagick and ffmpeg. Nothing
is mocked: every still is either the recorded `gpt-6-luna` hero run (first-visit autoplay) or a
live run against the local API (`gpt-6-luna` agent). The benchmark stills render the committed
`api/bench_results/report.json` and `learning_curve.json`.

## Setup

- Web on http://localhost:3001, API on http://localhost:8000 (already running). The plan prefers
  the production build (`npm run build && npm start`) so no dev indicator shows; the dev
  server works too, since the indicator is hidden in `next.config`.
- Each capture context is a **fresh isolated browser context**, so first-visit autoplay runs.
  Set the viewport with emulation, not window resizing: **1440x900 at device pixel ratio 2**
  (raw shots are 2880x1800).
- Raw screenshots go to `docs/media/.frames/` (the screenshot tool refuses paths outside the
  workspace). Delete that folder before committing.
- Budget (Oct 12 re-capture): 1 live console inject plus 2 Ask the graph questions, about $0.005. (Oct 10 run): **2 live LLM incidents** (one console inject, one blind judge inject),
  about $0.006. Everything else is free: the hero GIF is the recorded run, the challenge,
  X-ray, fragility map, Verify and storm make no LLM calls. No benchmark was run.
- Close every page you opened when done.

## Graph framing

The canvas fits the graph when its size changes. After a run, a focus animation can leave it
zoomed on one node, and the storm graph can stay unfitted. Before a still, nudge the viewport
(emulate 1440x880, then 1440x900) to refit.

## Stills

| File | How |
|---|---|
| `console-verdict.png` | Fresh context, `/`, **Inject fault** (`bad_deploy`), wait for LEARNED, refit, screenshot: graph, funnel, verdict, certificate with clause bars and onset strip. |
| `console-response.png` | Same run: scroll the Response region to the top of the right column. |
| `xray.png` | Same run: expand the `origin` funnel cut, click **X-ray**, scroll the plan table into view. |
| `challenge.png` | Same run: collapse the X-ray, open the drawer's **Was it my deploy?**, pick an eliminated change (one that fails `signature`), **Challenge**, open **Query that decided it**. Spends 1 of the incident's 2 challenges. |
| `proof-page.png` | Open the STATUS link (`/proof/<ref>`), click **Re-verify**. |
| `replay-scrubber.png` | `/replay/<incident id>`, set the range input to 100. (as-known is disabled when the incident has no late records.) |
| `storm.png` | `/`, **Storm**, wait for `done`, refit. |
| `judge-commit-reveal.png` | `/judge`, pick a service with many upstream services, **Commit and inject blind**, wait for LEARNED, **Reveal**. |
| `fragility.png` | Same round: **Find edits that flip the verdict**, scroll "Try these" to the top of the right column. |
| `judge-tamper-falsified.png` | Same round: click the first "Try these" edit, **Review edit**, **Confirm and write**, **Verify**, refit. |
| `benchmark.png` | `/benchmark`, scroll "Live LLM runs" to the top. |
| `why-wrong.png` | Open the gpt-6-luna **Why it was wrong** and its first **deciding query**; scroll the summary near the top. |
| `learning-curve.png` | Scroll "Learning curve" into view and screenshot that element (the region uid from a snapshot) at 1440x900 DPR 2; no crop needed. |
| `ask.png` | After a console run, open the drawer's **Ask the graph** tab, ask "Who owns checkout and who is on call right now?", wait for the badges (LLM, grounded), expand **Show queries**. The drawer is only 28vh tall: for the shot, set its height to about 62vh (`div.overflow-y-auto.border-t.border-rule`, style only) and scroll the answer to the top of the drawer. Costs about $0.001. A second, off-graph question ("What is the weather in Paris today?") shows the "no information" answer for the video script. |

## GIFs

- `console-investigation.gif` (hero): fresh context, `/`. Screenshot the first paint (autoplay
  already running), then pause and drive the scrubber: in the page,
  `Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set.call(scrub, n)` plus
  an `input` event, wait ~0.9 s, screenshot. Positions used: 8-13, 16, 19, 21, 23-27, 29, 33,
  37, 40, 41, with the last frame repeated 4 times (24 frames, 8 s).
- `judge-tamper-falsified.gif`: the frames of the tamper still sequence (Try these, the edit
  pre-filled, review, written, verifying, FALSIFIED zoomed on the responsible node, refit, the
  hypothesis chain with FALSIFIED and DRIFTED), each held 2-4 frames.

Assemble at 3 fps with a per-GIF palette:

```sh
ffmpeg -y -framerate 3 -i seq/f_%03d.png \
  -vf "scale=1200:-1:flags=lanczos,split[s0][s1];[s0]palettegen=max_colors=128[p];[s1][p]paletteuse=dither=bayer" \
  docs/media/console-investigation.gif
```

## Optimising stills

```sh
magick raw.png -strip -resize 2000x -colors 256 docs/media/out.png   # 130-270 KB each
```

## Checks

```sh
grep -o 'docs/media/[^")]*' README.md | sort -u | xargs ls -la   # every referenced file exists
rm -rf docs/media/.frames
```
