import type { Metadata } from "next";
import { BenchmarkView } from "@/components/BenchmarkTable";

export const metadata: Metadata = {
  title: "Benchmark · Incident Commander",
  description: "Accuracy, latency and tokens: graph proofs vs vector RAG vs full context, and FalkorDB vs a Postgres twin.",
};

export default function BenchmarkPage() {
  return <BenchmarkView />;
}
