import type { Metadata } from "next";
import { Suspense } from "react";
import { PageLoading } from "@/components/Shell";
import ReplayConsole from "@/components/ReplayConsole";

export const metadata: Metadata = {
  title: "Replay · Incident Commander",
  description: "Scrub through the outage: changes landing and services turning symptomatic, as it happened or as the agent knew it.",
};

// Next 16: dynamic segment params arrive as a Promise, so reading them sits inside Suspense.
async function Replay({ params }: { params: Promise<{ incident: string }> }) {
  const { incident } = await params;
  return <ReplayConsole incidentId={decodeURIComponent(incident)} />;
}

export default function ReplayPage({ params }: { params: Promise<{ incident: string }> }) {
  return (
    <Suspense fallback={<PageLoading label="Loading the replay" />}>
      <Replay params={params} />
    </Suspense>
  );
}
