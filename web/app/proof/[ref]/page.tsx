import type { Metadata } from "next";
import { Suspense } from "react";
import { PageLoading } from "@/components/Shell";
import ProofPage from "@/components/ProofPage";
import { pageMetadata } from "@/lib/site";

export const metadata: Metadata = {
  ...pageMetadata({
    title: "Proof",
    description:
      "The proof chain behind a rollback PR: every hypothesis, its Cypher query and hash, re-runnable against the PR's copy of the world.",
  }),
  robots: { index: false, follow: false },
};

// Next 16: dynamic segment params arrive as a Promise, so reading them sits inside Suspense.
async function Proof({ params }: { params: Promise<{ ref: string }> }) {
  const { ref } = await params;
  return <ProofPage proofRef={decodeURIComponent(ref)} />;
}

export default function ProofRoute({ params }: { params: Promise<{ ref: string }> }) {
  return (
    <Suspense fallback={<PageLoading label="Loading the proof" />}>
      <Proof params={params} />
    </Suspense>
  );
}
