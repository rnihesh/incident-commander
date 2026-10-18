import type { Metadata, Viewport } from "next";
import { Instrument_Sans, JetBrains_Mono } from "next/font/google";
import { AUTHOR, COLOR_PAPER, COLOR_VOID, SITE_DESCRIPTION, SITE_NAME, SITE_URL } from "@/lib/site";
import { THEME_BOOT_SCRIPT } from "@/lib/theme";
import "./globals.css";

const body = Instrument_Sans({ variable: "--font-body", subsets: ["latin"] });
const data = JetBrains_Mono({ variable: "--font-data", subsets: ["latin"] });

export const metadata: Metadata = {
  metadataBase: new URL(SITE_URL),
  title: { default: SITE_NAME, template: `%s · ${SITE_NAME}` },
  description: SITE_DESCRIPTION,
  applicationName: SITE_NAME,
  authors: [{ name: AUTHOR }],
  creator: AUTHOR,
  keywords: [
    "FalkorDB",
    "knowledge graph",
    "root cause analysis",
    "AI agent",
    "incident response",
    "Cypher",
    "on-call",
    "graph database",
    "GraphRAG",
    "SRE",
  ],
  openGraph: {
    type: "website",
    siteName: SITE_NAME,
    locale: "en_US",
    title: SITE_NAME,
    description: SITE_DESCRIPTION,
  },
  twitter: {
    card: "summary_large_image",
    title: SITE_NAME,
    description: SITE_DESCRIPTION,
  },
};

export const viewport: Viewport = {
  themeColor: [
    { media: "(prefers-color-scheme: light)", color: COLOR_PAPER },
    { media: "(prefers-color-scheme: dark)", color: COLOR_VOID },
  ],
  colorScheme: "dark light",
};

export default function RootLayout({ children }: LayoutProps<"/">) {
  return (
    // data-theme is set by the inline script before paint, so the server markup cannot match it.
    <html lang="en" className={`${body.variable} ${data.variable} h-full antialiased`} suppressHydrationWarning>
      <head>
        <script dangerouslySetInnerHTML={{ __html: THEME_BOOT_SCRIPT }} />
      </head>
      <body className="h-full overflow-hidden">{children}</body>
    </html>
  );
}
