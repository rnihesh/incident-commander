"use client";

import type { VerifyResult } from "@/lib/mock/verify";
import type { VerifyState } from "@/lib/payloads";
import { Badge, Button, type Tone } from "./ui";

const TONE_FOR: Record<VerifyState, Tone> = {
  VERIFIED: "verified",
  DRIFTED: "drifted",
  UNANCHORED: "info",
  FALSIFIED: "falsified",
  TAMPERED: "tampered",
};

export function VerifyBadge({ result, onFocus }: { result: VerifyResult; onFocus?: (node: string) => void }) {
  return (
    <span className="inline-flex flex-wrap items-center gap-2">
      <Badge tone={TONE_FOR[result.state]} title={result.detail}>
        {result.state === "UNANCHORED" ? "UNANCHORED \u2014 chain not yet anchored" : result.state}
      </Badge>
      {result.node && (
        <button
          type="button"
          onClick={() => result.node && onFocus?.(result.node)}
          className="font-mono text-meta text-dim underline decoration-dotted underline-offset-2 hover:text-ink"
          aria-label={`Responsible node ${result.node}. Focus on graph.`}
        >
          {result.node}
        </button>
      )}
    </span>
  );
}

export function VerifyButton({
  label = "Verify",
  running,
  disabled,
  result,
  onClick,
  onFocusNode,
  variant = "quiet",
}: {
  label?: string;
  running?: boolean;
  disabled?: boolean;
  result?: VerifyResult;
  onClick: () => void;
  onFocusNode?: (node: string) => void;
  /** quiet: a small secondary button in a row of proof actions; secondary: a panel's action;
   *  primary: the page's main action (the public proof page's Re-verify). */
  variant?: "quiet" | "secondary" | "primary";
}) {
  return (
    <span className="inline-flex items-center gap-2">
      <Button
        variant={variant === "primary" ? "primary" : "secondary"}
        size={variant === "quiet" ? "sm" : "md"}
        onClick={onClick}
        disabled={disabled || running}
        aria-busy={running}
      >
        {running ? "Re-running" : label}
      </Button>
      {result && !running && <VerifyBadge result={result} onFocus={onFocusNode} />}
    </span>
  );
}
