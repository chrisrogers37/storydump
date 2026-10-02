import { Bricolage_Grotesque } from "next/font/google"

// The marketing headlines. next/font downloads it at build time and serves it
// from this site, so a visitor's browser never contacts Google. Applied by the
// marketing layout, the 404 page and sign-in, not the root layout, so the app
// pages don't fetch it.
export const bricolage = Bricolage_Grotesque({
  variable: "--font-bricolage",
  subsets: ["latin"],
  weight: "800",
  // The H1 is the home page's largest paint. "optional" lets the metric-
  // matched fallback stand if the font is late, rather than repainting it.
  display: "optional",
})
