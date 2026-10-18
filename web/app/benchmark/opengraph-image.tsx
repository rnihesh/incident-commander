import { OG_SIZE, ogImage } from "@/components/OgCard";

export const alt =
  "Incident Commander benchmark: graph agent 50/50, full context 26/50, vector RAG 4/50 root causes found (same model)";
export const size = OG_SIZE;
export const contentType = "image/png";

export default function Image() {
  return ogImage({
    title: "Benchmark: 50 blind faults",
    tagline: "Graph proofs vs full context vs vector RAG, same model behind each.",
  });
}
