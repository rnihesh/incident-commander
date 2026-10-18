import type { Metadata } from "next";
import { BenchmarkView } from "@/components/BenchmarkTable";
import { pageMetadata } from "@/lib/site";

export const metadata: Metadata = pageMetadata({
  title: "Benchmark",
  description:
    "50 blind faults, same model behind every approach: the graph agent finds 50/50 root causes, " +
    "full context 26/50, vector RAG 4/50. Accuracy, latency and tokens, plus FalkorDB vs a Postgres twin.",
  path: "/benchmark",
  ownImage: true,
});

export default function BenchmarkPage() {
  return <BenchmarkView />;
}
