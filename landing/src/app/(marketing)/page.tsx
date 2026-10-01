import type { Metadata } from "next"
import { Hero } from "@/components/landing/hero"
import { SocialProof } from "@/components/landing/social-proof"
import { HowItWorks } from "@/components/landing/how-it-works"
import { TelegramPreview } from "@/components/landing/telegram-preview"
import { Features } from "@/components/landing/features"
import { Comparison } from "@/components/landing/comparison"
import { Pricing } from "@/components/landing/pricing"
import { FAQ } from "@/components/landing/faq"
import { FinalCTA } from "@/components/landing/final-cta"
import { StructuredData } from "@/components/landing/structured-data"

// The root layout's title, description and social card describe the home
// page; this only names it as its own canonical. No other page may inherit
// "/" (seo-contract.test.ts).
export const metadata: Metadata = {
  alternates: { canonical: "/" },
}

export default function Home() {
  return (
    <>
      <StructuredData />
      <Hero />
      <SocialProof />
      <HowItWorks />
      <TelegramPreview />
      <Features />
      <Comparison />
      <Pricing />
      <FAQ />
      <FinalCTA />
    </>
  )
}
