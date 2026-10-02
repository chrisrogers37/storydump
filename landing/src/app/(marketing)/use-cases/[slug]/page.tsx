import type { Metadata } from "next"
import Link from "next/link"
import { notFound } from "next/navigation"
import { WaitlistForm } from "@/components/landing/waitlist-form"
import { JsonLd } from "@/components/seo/json-ld"
import { breadcrumbList } from "@/lib/json-ld"
import { pageMetadata } from "@/lib/seo"
import { getUseCase, pathForUseCase, useCases, type UseCaseSlug } from "@/lib/use-cases"
import { evergreen } from "./_content/evergreen"
import { googleDrive } from "./_content/google-drive"
import { onlineStores } from "./_content/online-stores"
import { linkClass, type UseCaseContent } from "./_content/shared"
import { teamApprovals } from "./_content/team-approvals"

const content: Record<UseCaseSlug, UseCaseContent> = {
  "google-drive-to-instagram-stories": googleDrive,
  "evergreen-instagram-stories": evergreen,
  "approve-instagram-stories-in-telegram": teamApprovals,
  "instagram-stories-for-online-stores": onlineStores,
}

// The same four steps on every page; each links to the guide that covers it.
const setupSteps = [
  { text: "Join the waitlist. We’re inviting people in small batches, and we’ll email you when your spot is ready.", href: "#waitlist", link: "Join the waitlist" },
  { text: "Make sure your Instagram account is a professional account (Business or Creator).", href: "/setup/instagram", link: "Instagram account guide" },
  { text: "Put your photos and videos in Google Drive, one folder for each kind of Story.", href: "/setup/media-organize", link: "Folder guide" },
  { text: "Once you’re in, connect Instagram with its own login and Google Drive read-only, pick your folders, and set Stories a day, hours and time zone. Linking a Telegram group is optional.", href: "/setup", link: "The full setup guide" },
]

// An unknown slug is a 404 at routing, like the blog's.
export const dynamicParams = false

export function generateStaticParams() {
  return useCases.map(({ slug }) => ({ slug }))
}

type Params = Promise<{ slug: string }>

export async function generateMetadata({ params }: { params: Params }): Promise<Metadata> {
  const useCase = getUseCase((await params).slug)
  if (!useCase) return {}
  return pageMetadata({
    title: useCase.seoTitle,
    description: useCase.description,
    path: pathForUseCase(useCase.slug),
    ogTitle: useCase.ogTitle,
    ogSubtitle: useCase.ogSubtitle,
  })
}

function SignIn() {
  return (
    <>
      Already using Storydump?{" "}
      <Link href="/login" className={linkClass}>
        Sign in
      </Link>
    </>
  )
}

export default async function UseCasePage({ params }: { params: Params }) {
  const useCase = getUseCase((await params).slug)
  if (!useCase) notFound()
  const { slug } = useCase
  const { lede, visual, body } = content[slug]

  return (
    <>
      <JsonLd
        data={breadcrumbList([
          { name: "Home", path: "/" },
          { name: useCase.seoTitle, path: pathForUseCase(slug) },
        ])}
      />

      <section className="bg-paper py-14 md:py-20">
        <div className="mx-auto grid max-w-6xl items-center gap-12 px-4 lg:grid-cols-[1.25fr_1fr]">
          <div>
            <p className="kicker text-tap-ink">{useCase.eyebrow}</p>
            <h1 className="section-title mt-4 md:text-[4.25rem]">{useCase.title}</h1>
            <p className="mt-6 max-w-xl text-lg leading-relaxed text-ink/80">{lede}</p>
            <div className="mt-8 max-w-xl">
              <WaitlistForm variant="hero" />
            </div>
            <p className="mt-4 text-sm text-ink/70">
              Free during beta · No credit card required{" "}
              <span className="whitespace-nowrap">
                · <SignIn />
              </span>
            </p>
          </div>
          {visual}
        </div>
      </section>

      <div className="mx-auto max-w-3xl px-4 py-16 md:py-20">
        {body}

        <section className="mt-14">
          <h2 className="font-display text-3xl font-extrabold tracking-[-0.03em] text-ink md:text-4xl">
            Setup, in short
          </h2>
          <ol className="mt-6 space-y-4">
            {setupSteps.map((step, i) => (
              <li key={step.href} className="flex gap-4">
                <span
                  aria-hidden="true"
                  className="flex size-8 shrink-0 items-center justify-center rounded-full bg-ink font-mono text-sm text-white"
                >
                  {i + 1}
                </span>
                <p className="pt-1 leading-relaxed text-ink/80">
                  {step.text}{" "}
                  <Link href={step.href} className={linkClass}>
                    {step.link}
                  </Link>
                </p>
              </li>
            ))}
          </ol>
        </section>
      </div>

      <section aria-labelledby="closing-heading" className="bg-tap py-16 md:py-20">
        <div className="mx-auto max-w-3xl px-4 text-center">
          <h2 id="closing-heading" className="section-title">
            {useCase.closing}
          </h2>
          <p className="mt-5 font-medium text-ink">Free during beta · No credit card required.</p>
          <div className="mx-auto mt-8 max-w-lg">
            <WaitlistForm variant="footer" />
          </div>
          <p className="mt-6 text-sm text-ink">
            <SignIn />
          </p>
        </div>
      </section>

      <nav aria-labelledby="more-heading" className="py-14">
        <div className="mx-auto max-w-3xl px-4">
          <h2 id="more-heading" className="kicker text-ink/60">
            More use cases
          </h2>
          <ul className="mt-4 flex flex-wrap gap-3">
            {useCases
              .filter((other) => other.slug !== slug)
              .map((other) => (
                <li key={other.slug}>
                  <Link
                    href={pathForUseCase(other.slug)}
                    className="inline-flex rounded-full border border-ink/15 px-4 py-2 text-sm font-semibold text-ink transition-colors hover:border-tap-ink hover:text-tap-ink"
                  >
                    {other.navLabel}
                  </Link>
                </li>
              ))}
          </ul>
        </div>
      </nav>
    </>
  )
}
