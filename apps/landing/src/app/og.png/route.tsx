import { ImageResponse } from "next/og";

import { SITE } from "@/lib/site";

// Written to a plain file at build time, as a static export needs.
export const dynamic = "force-static";

// /og.png: the picture X, LinkedIn and chat apps show when someone shares the link; drawn once at build time. A route of its own (not opengraph-image.tsx) so the exported file keeps its .png name and static hosts send the right type.
const size = { width: 1200, height: 630 };

export function GET() {
  return new ImageResponse(
    <div
      style={{
        width: "100%",
        height: "100%",
        display: "flex",
        flexDirection: "column",
        justifyContent: "space-between",
        background: "#141414",
        borderTop: "12px solid #f5c518",
        padding: "64px 72px",
        color: "#ececec",
      }}
    >
      <div style={{ display: "flex", alignItems: "center", gap: 16, fontSize: 32, fontWeight: 700 }}>
        <div style={{ width: 28, height: 28, background: "#f5c518" }} />
        {SITE.name}
      </div>
      <div style={{ display: "flex", flexDirection: "column", gap: 20 }}>
        <div style={{ fontSize: 76, fontWeight: 700, lineHeight: 1.05, letterSpacing: "-0.02em" }}>
          See how an AI agent works, step by step.
        </div>
        <div style={{ fontSize: 30, color: "#9a9a9a" }}>The loop, every tool call and its cost, on screen.</div>
      </div>
      <div style={{ display: "flex", gap: 0 }}>
        {["you ask", "model replies", "tools run", "results go back"].map((step) => (
          <div
            key={step}
            style={{ border: "2px solid #2e2e2e", background: "#1c1c1c", padding: "14px 22px", fontSize: 22, color: "#f5c518", textTransform: "uppercase" }}
          >
            {step}
          </div>
        ))}
      </div>
    </div>,
    size,
  );
}
