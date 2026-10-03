import { Bricolage_Grotesque, Geist, Geist_Mono } from "next/font/google"

// The site's three faces, applied once by the root layout so the marketing
// site, sign-in and the app share them. next/font downloads them at build time
// and serves them from this site, so a visitor's browser never contacts
// Google. tokens.css maps them to roles: Geist is font-sans (body), Geist Mono
// is font-mono (kickers), Bricolage is font-display (headings).

const geistSans = Geist({
  variable: "--font-geist-sans",
  subsets: ["latin"],
})

const geistMono = Geist_Mono({
  variable: "--font-geist-mono",
  subsets: ["latin"],
  // Small labels only: not worth a high-priority download ahead of the H1.
  preload: false,
})

// One instance, "swap". There used to be an "optional" copy for the home H1
// beside a "swap" copy for the 404 page (which gets no font preload, so
// "optional" left its headline in the fallback). Both declared the same family
// in the same global stylesheet, so the later "swap" rules won on every page
// anyway; this states what the site was already doing.
const bricolage = Bricolage_Grotesque({
  variable: "--font-bricolage",
  subsets: ["latin"],
  weight: "800",
  display: "swap",
})

/** The class that defines all three font variables; the root layout puts it on <body>. */
export const fontVariables = `${geistSans.variable} ${geistMono.variable} ${bricolage.variable}`
