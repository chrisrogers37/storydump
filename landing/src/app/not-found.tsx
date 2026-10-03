import type { Metadata } from "next"
import Link from "next/link"
import { Header } from "@/components/layout/header"
import { Footer } from "@/components/layout/footer"
import { buttonVariants } from "@/components/ui/button"

export const metadata: Metadata = {
  title: "Page not found",
  description: "This page isn’t on the Storydump site.",
  // Overrides the root layout's "index, follow"; its links are still worth following.
  robots: { index: false, follow: true },
}

/**
 * Every unmatched URL, and every `notFound()` call, lands here. A stale link
 * is often someone's first visit, so the page keeps the site's header and
 * footer and offers a way on rather than a dead end.
 */
export default function NotFound() {
  return (
    <div className="flex min-h-svh flex-col">
      <Header />
      <main id="main" className="flex-1 bg-paper">
        <div className="mx-auto max-w-3xl px-4 py-24 md:py-32">
          <p className="kicker text-tap-ink">404</p>
          <h1 className="section-title mt-4">That page isn’t here.</h1>
          <p className="mt-6 max-w-xl text-lg text-ink/80">
            The link may be old, or the address may have a typo. Here’s where
            to go instead.
          </p>
          <ul className="mt-10 flex flex-wrap gap-3">
            <li>
              <Link
                href="/"
                className={buttonVariants({ size: "xl" })}
              >
                Go to the home page
              </Link>
            </li>
            <li>
              <Link
                href="/blog"
                className={buttonVariants({ variant: "outline", size: "xl" })}
              >
                Read the blog
              </Link>
            </li>
            <li>
              <Link
                href="/setup"
                className={buttonVariants({ variant: "outline", size: "xl" })}
              >
                See the setup guide
              </Link>
            </li>
          </ul>
        </div>
      </main>
      <Footer />
    </div>
  )
}
