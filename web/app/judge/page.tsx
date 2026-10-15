import type { Metadata } from "next";
import { Suspense } from "react";
import { PageLoading } from "@/components/Shell";
import JudgeConsole from "@/components/JudgeConsole";

export const metadata: Metadata = {
  title: "Judge mode · Incident Commander",
  description: "Plant a fault the agent cannot see, reveal the commitment, then tamper with the graph and re-run the proofs.",
};

export default function JudgePage() {
  return (
    <Suspense fallback={<PageLoading label="Loading judge mode" />}>
      <JudgeConsole />
    </Suspense>
  );
}
