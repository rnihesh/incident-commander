import type { Metadata } from "next";
import { Suspense } from "react";
import { PageLoading } from "@/components/Shell";
import Console from "@/components/Console";

export const metadata: Metadata = {
  alternates: { canonical: "/" },
};

export default function Home() {
  return (
    <Suspense fallback={<PageLoading label="Loading the console" />}>
      <Console />
    </Suspense>
  );
}
