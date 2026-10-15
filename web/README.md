# Incident Commander Web

Next.js 16 console for Incident Commander: watch an investigation stream in, inspect the
proofs, and try to break them.

See the [root README](../README.md) for the full story and setup.

## Run

```bash
pnpm install
pnpm dev          # http://localhost:3001, expects the API on http://localhost:8000
```

Set `NEXT_PUBLIC_API_URL` if the API runs elsewhere.

## Pages

| Route | What it shows |
|---|---|
| `/` | Investigation console: reflex answer, elimination funnel, clause-bar certificate, response, Ask-the-graph drawer |
| `/judge` | Judge chaos monkey: blind commit-reveal fault, tamper panel, live re-verify that flips proofs to FALSIFIED |
| `/benchmark` | Benchmark results, learning and forgetting curves |
| `/replay/[incident]` | Time machine: scrub the graph as known at each moment of the incident |
| `/proof/[ref]` | A single proof with its Cypher, ledger hash and Verify button |

## Layout

- `app/` routes, `components/` UI (Funnel, Certificate, TamperPanel, Storm, ProofPage, ...)
- `lib/` API client, SSE stream, run reducer, autoplay and mock fixtures
- `tests/` vitest suites

## Test

```bash
pnpm test
pnpm lint
```
