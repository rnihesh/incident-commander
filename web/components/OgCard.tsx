import { readFile } from "node:fs/promises";
import { join } from "node:path";
import { ImageResponse } from "next/og";
import { Mark } from "./Mark";

/* Shared Open Graph / Twitter card (1200x630), rendered once at build time by next/og.
   Fonts are vendored TTFs (next/og cannot read the woff2 files next/font serves), so the
   build needs no network. Colours are the tokens in app/globals.css. */

export const OG_SIZE = { width: 1200, height: 630 };

const C = {
  void: "#090d12",
  deck: "#0e141b",
  rule: "#202d3c",
  ink: "#d7e0ea",
  dim: "#8394a8",
  faint: "#526379",
  verified: "#3ee59b",
  drifted: "#f2b134",
  falsified: "#ff5a5f",
};

const fontDir = join(process.cwd(), "assets/fonts");
const fonts = Promise.all([
  readFile(join(fontDir, "InstrumentSans-Regular.ttf")),
  readFile(join(fontDir, "InstrumentSans-SemiBold.ttf")),
  readFile(join(fontDir, "JetBrainsMono-Medium.ttf")),
]);

const RESULTS = [
  { label: "Graph agent", score: 50, color: C.verified },
  { label: "Full context", score: 26, color: C.drifted },
  { label: "Vector RAG", score: 4, color: C.falsified },
];

export async function ogImage({ title, tagline }: { title: string; tagline: string }) {
  const [regular, semibold, mono] = await fonts;
  return new ImageResponse(
    (
      <div
        style={{
          width: "100%",
          height: "100%",
          display: "flex",
          flexDirection: "column",
          justifyContent: "space-between",
          padding: "64px 72px",
          background: C.void,
          color: C.ink,
          fontFamily: "Instrument Sans",
          borderTop: `6px solid ${C.verified}`,
        }}
      >
        <div style={{ display: "flex", flexDirection: "column" }}>
          <div style={{ display: "flex", alignItems: "center", gap: 20 }}>
            <Mark size={72} />
            <div style={{ display: "flex", fontFamily: "JetBrains Mono", fontSize: 24, color: C.dim }}>
              incident-commander · on FalkorDB
            </div>
          </div>
          <div style={{ display: "flex", marginTop: 40, fontSize: 76, fontWeight: 600, letterSpacing: -1.5, lineHeight: 1.05 }}>
            {title}
          </div>
          <div style={{ display: "flex", marginTop: 18, fontSize: 32, color: C.dim, lineHeight: 1.3 }}>{tagline}</div>
        </div>

        <div
          style={{
            display: "flex",
            flexDirection: "column",
            padding: "26px 32px",
            background: C.deck,
            border: `1px solid ${C.rule}`,
          }}
        >
          <div style={{ display: "flex", gap: 40 }}>
            {RESULTS.map((r) => (
              <div key={r.label} style={{ display: "flex", flexDirection: "column", flex: 1 }}>
                <div style={{ display: "flex", alignItems: "baseline", justifyContent: "space-between" }}>
                  <div style={{ display: "flex", fontSize: 26, color: C.ink }}>{r.label}</div>
                  <div style={{ display: "flex", fontFamily: "JetBrains Mono", fontSize: 34, color: r.color }}>
                    {`${r.score}/50`}
                  </div>
                </div>
                <div style={{ display: "flex", marginTop: 12, height: 10, background: C.rule }}>
                  <div style={{ display: "flex", width: `${(r.score / 50) * 100}%`, background: r.color }} />
                </div>
              </div>
            ))}
          </div>
          <div style={{ display: "flex", marginTop: 18, fontFamily: "JetBrains Mono", fontSize: 19, color: C.dim }}>
            root causes found in 50 blind faults · same model behind every approach
          </div>
        </div>
      </div>
    ),
    {
      ...OG_SIZE,
      fonts: [
        { name: "Instrument Sans", data: regular, weight: 400, style: "normal" },
        { name: "Instrument Sans", data: semibold, weight: 600, style: "normal" },
        { name: "JetBrains Mono", data: mono, weight: 500, style: "normal" },
      ],
    },
  );
}
