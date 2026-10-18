import type { Metadata } from "next";
import type { ReactNode } from "react";

// The owner's usage panel: never indexed, never linked from the public pages.
export const metadata: Metadata = {
  title: "Admin · Incident Commander",
  robots: { index: false, follow: false },
};

export default function AdminLayout({ children }: { children: ReactNode }) {
  return children;
}
