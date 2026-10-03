import { Bricolage_Grotesque } from "next/font/google"

// The site's headings. next/font downloads it at build time and serves it from
// this site, so a visitor's browser never contacts Google. Applied by the root
// layout, so the marketing site, sign-in and the app share it.
//
// One instance, "swap". There used to be an "optional" copy for the home H1
// beside a "swap" copy for the 404 page (which gets no font preload, so
// "optional" left its headline in the fallback). Both declare the same family
// in the same global stylesheet, so the later "swap" rules won on every page
// anyway; this states what the site was already doing.
export const bricolage = Bricolage_Grotesque({
  variable: "--font-bricolage",
  subsets: ["latin"],
  weight: "800",
  display: "swap",
})
