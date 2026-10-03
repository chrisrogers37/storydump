import type { Metadata } from "next"
import Link from "next/link"
import { ArrowRight } from "lucide-react"
import { FinalCTA } from "@/components/landing/final-cta"
import { HeroSignup } from "@/components/landing/hero-signup"
import { JsonLd } from "@/components/seo/json-ld"
import { breadcrumbList } from "@/lib/json-ld"
import { pageMetadata } from "@/lib/seo"
import { pathForUseCase, useCases, useCasesPath } from "@/lib/use-cases"

export const metadata: Metadata = pageMetadata({
  title: "Instagram Stories Use Cases",
  description:
    "How small teams use Storydump for Instagram Stories: from Google Drive, evergreen content, team approvals in Telegram and daily Stories for shops.",
  path: useCasesPath,
  ogTitle: "One library. A Story every day.",
  ogSubtitle: "From Google Drive, evergreen content, team approvals in Telegram and online stores.",
})

export default function UseCasesIndex() {
  return (
    <>
      <JsonLd
        data={breadcrumbList([
          { name: "Home", path: "/" },
          { name: "Use cases", path: useCasesPath },
        ])}
      />

      <section className="bg-paper py-14 md:py-20">
        <div className="mx-auto max-w-6xl px-4">
          <p className="kicker text-tap-ink">Use cases</p>
          <h1 className="section-title mt-4 max-w-4xl md:text-[4.25rem]">
            One library. A Story every day.
          </h1>
          <p className="mt-6 max-w-2xl text-lg leading-relaxed text-ink/80">
            Storydump picks today’s Story from your Google Drive, gets it ready
            and brings it to your team right on time. Here’s how that plays out
            for the way you post.
          </p>
          <HeroSignup />
        </div>
      </section>

      <div className="mx-auto max-w-6xl px-4 py-16 md:py-20">
        <ul className="grid gap-6 md:grid-cols-2">
          {useCases.map((useCase) => (
            <li key={useCase.slug}>
              <Link
                href={pathForUseCase(useCase.slug)}
                className="group flex h-full flex-col rounded-2xl border border-ink/10 bg-white p-6 transition-colors hover:border-tap-ink md:p-8"
              >
                <p className="kicker text-tap-ink">{useCase.navLabel}</p>
                <h2 className="mt-3 font-display text-2xl font-extrabold tracking-[-0.03em] text-ink md:text-3xl">
                  {useCase.title}
                </h2>
                <p className="mt-3 flex-1 leading-relaxed text-ink/80">{useCase.description}</p>
                <span className="mt-5 inline-flex items-center gap-1 text-sm font-semibold text-ink group-hover:text-tap-ink">
                  See how it works <ArrowRight aria-hidden="true" className="size-4" />
                </span>
              </Link>
            </li>
          ))}
        </ul>
      </div>

      <FinalCTA headingClassName="" />
    </>
  )
}
