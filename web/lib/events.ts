// Mirror of api/src/ic/api/events.py. Keep both in sync.

export const SCHEMA_VERSION = 1;

export type EventType =
  | "run_started"
  | "reflex"
  | "snapshot"
  | "tool_call"
  | "hypothesis"
  | "proof"
  | "funnel"
  | "certificate"
  | "verdict"
  | "state"
  | "handoff"
  | "rehearsal"
  | "action"
  | "memory"
  | "narration"
  | "storm"
  | "error"
  | "done";

export type AgentName = "system" | "triage" | "remediation";

export interface RunEvent<T = Record<string, unknown>> {
  id: number;
  run_id: string;
  ts: number;
  type: EventType;
  agent: AgentName;
  data: T;
  schema_version: number;
}

export interface ToolResult {
  tool: string;
  result: unknown;
  cypher: string[];
  params: Record<string, unknown>[];
  nodes_touched: string[];
  ms: number;
  graph: string;
}
