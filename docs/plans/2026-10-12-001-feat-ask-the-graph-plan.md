---
title: Ask the Graph - Plan
type: feat
date: 2026-10-12
topic: ask-the-graph
artifact_contract: ce-unified-plan/v1
artifact_readiness: implementation-ready
product_contract_source: ce-plan-bootstrap
execution: code
---

# Ask the Graph - Plan

## Goal Capsule

- **Objective:** Let anyone ask plain-English questions about the company in the graph (people, teams, services, changes, incidents, postmortems, decisions) and get an answer whose every claim links to the graph nodes and Cypher that support it. Also ship a polish and small-screen pass.
- **Product authority:** This plan. User chose "Polish + mobile" and "Ask the graph" on 2026-10-12 (session-settled: user-approved — chosen over the guided tour: covers the Company Brain track and fixes visible demo rough edges).
- **Open blockers:** None.
- **Stop conditions:** Stop if a change would alter investigation logic, proof hashing, ledger format, verdict gate, or an existing endpoint's behavior; stop before any paid-LLM benchmark.
- **Execution profile:** U1 and U2 in parallel (U1 backend + its web panel in a worktree; U2 polish in the main checkout with a browser). U3 docs/media last.
- **Tail ownership:** ce-work style: orchestrator merges, verifies, commits on `feat/incident-commander`; no push (no remote).

## Product Contract

### Summary

An "Ask" panel answers questions like "who owns checkout and who is on call?", "which changes by the payments team caused incidents?", "what did we decide after the last search outage?" using read-only graph tools, and shows each claim's sources as clickable nodes plus the Cypher that produced them.

### Problem Frame

The Company Brain track asks for an agent that "understands how a company works by connecting people, projects, documents, and decisions in one graph", with answers that have clear sources. The world graph already holds that company (Team, Person, Service, Change, Incident, Postmortem, Recommendation, ADR), but the product only exposes it through incident investigation.

### Requirements

**Asking**
- R1. A visitor can ask a free-text question (≤ 500 chars) from a new "Ask" tab in the console drawer; the answer arrives in a few seconds with a loading state.
- R2. Every answer lists its sources: the graph nodes it relied on (clickable: focus the node on the graph when it is in view) and the Cypher of each tool call (expandable).
- R3. Answers are grounded: a cited node must appear in the nodes the tool calls actually touched; uncited or unsupported claims are not presented as facts. When the graph has no answer, the panel says so.
- R4. Questions are read-only: asking never writes to any graph or the ledger.
- R5. Without an LLM key, the panel still answers a fixed set of question shapes (owner of X, who is on call for X, recent changes to X, incidents on X, decisions/recommendations for X) via a deterministic router, and says it is in limited mode.
- R6. Asking is bounded: per-session rate limit and the existing spend breaker; long questions rejected with a clear message.

**Agent access**
- R7. The new company tools are read-only registry tools, so MCP exposes them automatically; MCP instructions mention them.

**Polish and small screens**
- R8. X-ray query text wraps instead of clipping; the graph refits when a run settles and after a focus animation ends; the advisory banner does not cover graph labels; learning-curve "÷2" labels do not overlap markers.
- R9. Console, judge, benchmark, proof and replay pages are usable at 1280x800, 1024x768, 768x1024 and 390x844: panels stack, no horizontal page scroll, controls reachable, text not clipped.

### Acceptance Examples

- AE1. **Covers R1, R2, R3.** "Who owns svc-checkout-api@us-east and who is on call now?" → names the team and the on-call person; sources include the Service, Team and Person nodes and the queries.
- AE2. **Covers R3.** "What is the CEO's favorite color?" → "The graph has no information about that", no sources.
- AE3. **Covers R5.** With no key, "recent changes to checkout" returns the latest changes with authors via the router and a "limited mode" note.
- AE4. **Covers R6.** The 21st question in an hour from one session returns 429 with Retry-After.

### Scope Boundaries

- No writes, no actions from Ask; no conversation memory beyond the current question (single-turn, with an optional "focus incident" context).
- No new LLM provider work; uses the existing configured provider (gpt-6-luna) and fallback.

## Planning Contract

### Key Technical Decisions

- KTD1. **Company tools are new read-only registry tools on the world graph** (`target="world"`): `find_entities` (fuzzy full-text over Service/Team/Person/ADR/Postmortem names and ids), `service_profile`, `team_profile`, `person_profile`, `decisions_for` (ADRs and postmortem recommendations with status), `incident_history`. Each returns evidence (`cypher`, `params`, `nodes_touched`) like existing tools. Governs R2, R3, R4, R7.
- KTD2. **The answer loop reuses `agent/loop.py`** with an allowlist of the company tools plus existing read-only `who_is_on_call`, `observed_calls`, `find_precedents`, `recall`, and a terminal `submit_answer(answer, citations[])` tool. Governs R1, R3.
- KTD3. **Grounding guard is server-side:** citations not in the union of `nodes_touched` are dropped; if none remain and the answer asserts facts, the response is marked `ungrounded` and the UI shows it as unverified. Governs R3.
- KTD4. **Deterministic router fallback** (no key or LLM failure): regex intents → one or two tool calls → templated answer with sources; response flagged `mode: "limited"`. Governs R5.
- KTD5. **Endpoint** `POST /api/ask {question, incident_id?}`: session-scoped, JSON-only, `extra=forbid`; per-session limit (default 20/hour, setting), spend breaker; response `{answer, mode: "llm"|"limited", grounded, citations:[{id,label,kind}], calls:[{tool, cypher, params}], ms, tokens}`. Governs R1, R6.

## Implementation Units

### U1. Ask the graph (backend + panel)

**Goal:** R1-R7.
**Files:** `api/src/ic/tools/company.py` (new), `api/src/ic/tools/registry.py`, `api/src/ic/agent/ask.py` (new), `api/src/ic/agent/prompts.py` (ask prompt), `api/src/ic/api/routes_ask.py` (new), `api/src/ic/api/app.py`, `api/src/ic/config.py`, `api/src/ic/sessions/limits.py`, `api/src/ic/mcp_server.py`, `docs/notes/mcp-client-setup.md`, `api/tests/tools/test_company.py`, `api/tests/agent/test_ask.py`, `api/tests/api/test_ask.py`, `api/tests/tools/test_registry.py`, `api/tests/tools/test_investigate.py`, `api/tests/test_mcp_server.py`, `web/lib/ask.ts`, `web/lib/api.ts`, `web/components/Ask.tsx`, `web/components/Console.tsx` (drawer tab), `web/tests/ask.test.ts`.
**Approach:** per KTD1-KTD5. Use `ScriptedLLM` for loop tests; never call a paid LLM in tests.
**Test scenarios:**
- Covers AE1. Scripted LLM calls `find_entities` then `service_profile` then `submit_answer` citing team and person ids → response grounded with those citations and both queries.
- Covers AE2. Scripted answer with no citations for an off-graph question → `grounded: false` shown as "no information".
- Citation not touched by any call is dropped.
- Covers AE3. No LLM: router answers owner, on-call, recent changes, incidents, decisions shapes; unknown shape returns a helpful "try asking…" message.
- Covers AE4. 21st call → 429 with Retry-After; > 500 chars → 422; non-JSON → 415.
- Each company tool returns evidence fields and touches only world graph nodes; no graph writes (node/edge counts unchanged).
- Registry/MCP tool lists include the new tools.
- Web: answer rendering with source chips and expandable queries; limited-mode note; ungrounded state.
**Verification:** asking AE1 live in the console returns a grounded answer with clickable sources.

### U2. Polish and small screens

**Goal:** R8, R9.
**Files:** `web/components/ui.tsx`, `web/components/GraphCanvas.tsx`, `web/components/GraphCanvasInner.tsx`, `web/components/LearningCurve.tsx`, `web/components/RecommendationBanner.tsx`, `web/components/Console.tsx`, `web/components/JudgeConsole.tsx`, `web/components/ReplayConsole.tsx`, `web/components/BenchmarkTable.tsx`, `web/components/ProofPage.tsx`, `web/app/**`.
**Test scenarios:** Test expectation: existing render tests stay green; pure helpers get tests where extracted.
**Verification:** browser check at the four sizes on every page; no horizontal page scroll; screenshots inspected.

### U3. Docs and media

**Goal:** README "Ask the graph" feature, Company Brain track row, screenshot `docs/media/ask.png`, MCP notes, video-script beat.
**Dependencies:** U1, U2.

## Verification Contract

- `cd api && uv run ruff check src tests && uv run ruff format --check src tests && uv run pytest -q`
- `cd web && npx vitest run && npx tsc --noEmit && npx eslint . && npm run build`
- Browser checks via Chrome DevTools MCP; close pages afterwards.

## Definition of Done

- R1-R9 met; verification passes on the final commit; README media includes the Ask panel; no paid-LLM test calls; no abandoned code.
