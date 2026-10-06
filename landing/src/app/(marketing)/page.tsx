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
import { homeSocial } from "@/lib/seo"

// The root layout's title, description and social card describe the home
// page; this names it as its own canonical and og:url. No other page may
// inherit "/" (seo-contract.test.ts).
export const metadata: Metadata = {
  alternates: { canonical: "/" },
  openGraph: { ...homeSocial.openGraph, url: "/" },
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
