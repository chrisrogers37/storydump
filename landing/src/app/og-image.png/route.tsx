import { ImageResponse } from "next/og"
import type { NextRequest } from "next/server"

export const runtime = "edge"

// Direction A's palette (globals.css: paper, ink, tap). The card is drawn with
// next/og's bundled font, so it fetches nothing from another origin.
const PAPER = "#f4f1ea"
const INK = "#15130f"
const TAP = "#ff5a1f"

export async function GET(request: NextRequest) {
  const { searchParams } = request.nextUrl
  // Capped so a hand-made URL can't overflow the card; the site's own
  // titles and subtitles fit well inside these.
  const title = (searchParams.get("title") || "Instagram Stories, on tap").slice(0, 70)
  const subtitle = (
    searchParams.get("subtitle") ||
    "Stories from your Google Drive, brought to your team and posted with one tap."
  ).slice(0, 110)

  return new ImageResponse(
    (
      <div
        style={{
          height: "100%",
          width: "100%",
          display: "flex",
          flexDirection: "column",
          justifyContent: "space-between",
          padding: "72px 80px",
          backgroundColor: PAPER,
          color: INK,
        }}
      >
        <div style={{ display: "flex", alignItems: "center", gap: "16px" }}>
          <div
            style={{
              width: "44px",
              height: "44px",
              borderRadius: "9999px",
              border: `8px solid ${TAP}`,
              display: "flex",
              alignItems: "center",
              justifyContent: "center",
            }}
          >
            <div
              style={{
                width: "12px",
                height: "12px",
                borderRadius: "9999px",
                backgroundColor: INK,
              }}
            />
          </div>
          <div style={{ fontSize: "36px", fontWeight: 800, letterSpacing: "-0.03em" }}>
            Storydump
          </div>
        </div>
        <div style={{ display: "flex", flexDirection: "column", gap: "28px" }}>
          <div
            style={{
              display: "flex",
              fontSize: title.length > 40 ? "68px" : "84px",
              fontWeight: 800,
              letterSpacing: "-0.04em",
              lineHeight: 1,
              maxWidth: "1000px",
            }}
          >
            {title}
          </div>
          <div
            style={{
              display: "flex",
              fontSize: "30px",
              lineHeight: 1.35,
              color: "rgba(21, 19, 15, 0.75)",
              maxWidth: "940px",
            }}
          >
            {subtitle}
          </div>
        </div>
        <div style={{ display: "flex", height: "12px", width: "160px", borderRadius: "9999px", backgroundColor: TAP }} />
      </div>
    ),
    {
      width: 1200,
      height: 630,
    }
  )
}
