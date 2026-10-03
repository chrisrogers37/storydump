import { Header } from "@/components/layout/header"
import { Footer } from "@/components/layout/footer"
import { bricolage } from "@/lib/fonts"

export default function MarketingLayout({
  children,
}: {
  children: React.ReactNode
}) {
  return (
    // The root layout defines Geist as a variable on <body> but nothing
    // applies it, so text fell back to the system font; the marketing pages
    // set it here.
    <div className={`${bricolage.variable} flex min-h-svh flex-col font-sans`}>
      <Header />
      <main id="main" className="flex-1">{children}</main>
      <Footer />
    </div>
  )
}
