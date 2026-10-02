import { Bricolage_Grotesque } from "next/font/google"
import { Header } from "@/components/layout/header"
import { Footer } from "@/components/layout/footer"

// The marketing headlines. next/font downloads it at build time and serves it
// from this site, so a visitor's browser never contacts Google. Loaded here,
// not in the root layout, so the app and auth pages don't fetch it.
const bricolage = Bricolage_Grotesque({
  variable: "--font-bricolage",
  subsets: ["latin"],
  weight: "800",
  // The H1 is the home page's largest paint. "optional" lets the metric-
  // matched fallback stand if the font is late, rather than repainting it.
  display: "optional",
})

export default function MarketingLayout({
  children,
}: {
  children: React.ReactNode
}) {
  return (
    // The root layout defines Geist as a variable on <body> but nothing
    // applies it, so text fell back to the system font; the marketing pages
    // set it here.
    <div className={`${bricolage.variable} font-sans`}>
      <Header />
      <main>{children}</main>
      <Footer />
    </div>
  )
}
