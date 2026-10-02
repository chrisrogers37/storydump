import type { Metadata } from "next"
import { Hero } from "@/components/landing/hero"
import { Chores } from "@/components/landing/chores"
import { WhereTap } from "@/components/landing/where-tap"
import { SetItOnce } from "@/components/landing/set-it-once"
import { WhoItsFor } from "@/components/landing/who-its-for"
import { TrustRow } from "@/components/landing/trust-row"
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
      <Chores />
      <WhereTap />
      <SetItOnce />
      <WhoItsFor />
      <TrustRow />
      <FAQ />
      <FinalCTA />
    </>
  )
}
