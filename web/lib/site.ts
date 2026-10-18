import type { Metadata } from "next";

/** Canonical site origin and shared copy for metadata, robots, sitemap and the OG images. */
export const SITE_URL = process.env.NEXT_PUBLIC_SITE_URL ?? "https://commander.niheshr.com";
export const SITE_NAME = "Incident Commander";
export const SITE_TAGLINE = "An on-call agent whose every answer is a proof you can re-run or break.";
export const SITE_DESCRIPTION =
  "An on-call AI agent on FalkorDB that does graph-based root-cause analysis. Every answer is a " +
  "stored Cypher proof anyone can re-run or break. It rehearses rollbacks on cloned graphs, then " +
  "acts and remembers.";
export const AUTHOR = "Nihesh Rachakonda";
/** Theme colour from app/globals.css (--color-void). */
export const COLOR_VOID = "#090d12";

export const OG_ALT =
  "Incident Commander: graph agent 50/50, full context 26/50, vector RAG 4/50 root causes found (same model)";
/** The root card (app/opengraph-image.tsx), reused by pages without their own image file. */
const DEFAULT_CARD = { url: "/opengraph-image", width: 1200, height: 630, alt: OG_ALT };

/** Title, description and Open Graph / Twitter text for one page. A page's `openGraph` replaces
 *  the root layout's object wholesale (images included), so the shared fields are repeated here. */
export function pageMetadata({
  title,
  description,
  path,
  ownImage = false,
}: {
  title: string;
  description: string;
  /** Canonical path; omit for per-incident pages whose path is only known at request time. */
  path?: string;
  /** The segment has its own opengraph-image / twitter-image files; config images would win over them. */
  ownImage?: boolean;
}): Metadata {
  const full = `${title} · ${SITE_NAME}`;
  // Even `images: undefined` hides the segment's own image files, so leave the key out there.
  const images = ownImage ? {} : { images: DEFAULT_CARD };
  return {
    title,
    description,
    ...(path ? { alternates: { canonical: path } } : {}),
    openGraph: {
      type: "website",
      siteName: SITE_NAME,
      locale: "en_US",
      url: path,
      title: full,
      description,
      ...images,
    },
    twitter: { card: "summary_large_image", title: full, description, ...images },
  };
}
