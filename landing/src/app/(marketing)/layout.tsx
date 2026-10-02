import { Header } from "@/components/layout/header"
import { Footer } from "@/components/layout/footer"

export default function MarketingLayout({
  children,
}: {
  children: React.ReactNode
}) {
  return (
    // The root layout defines Geist as a variable on <body> but nothing
    // applies it, so text fell back to the system font; the marketing pages
    // set it here.
    <div className="font-sans">
      <Header />
      <main>{children}</main>
      <Footer />
    </div>
  )
}
