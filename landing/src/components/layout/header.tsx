import Link from "next/link"
import { TrackedLink } from "@/components/analytics/tracked-link"
import { buttonVariants } from "@/components/ui/button"
import { Wordmark } from "@/design/brand"
import { WaitlistLink } from "./waitlist-link"

/** The header's text links: muted, darkening on hover. */
const navLinkClass = "whitespace-nowrap py-2.5 text-sm text-muted-foreground transition-colors hover:text-foreground"

export function Header() {
  return (
    <header className="sticky top-0 z-50 w-full border-b border-ink/10 bg-white/85 backdrop-blur-sm">
      {/* The first stop for a keyboard: past the header to the page itself. */}
      <a
        href="#main"
        className={buttonVariants({ className: "sr-only focus:not-sr-only focus:absolute focus:left-4 focus:top-3 focus:z-50 focus:h-9 focus:px-4 focus:py-2" })}
      >
        Skip to content
      </a>
      <div className="mx-auto flex h-16 max-w-6xl items-center justify-between px-4">
        <Link href="/" className="flex shrink-0 pr-2 text-lg sm:pr-0 sm:text-xl">
          <Wordmark />
        </Link>
        <div className="flex items-center gap-2 sm:gap-4">
          {/*
            The wordmark, Blog, Sign in and the waitlist button fit a 360px
            phone with nothing hidden, deliberately: at 390px the wordmark
            collided with Blog and "Sign in" wrapped onto two lines (#1090 A1),
            so they were tightened until all four fit rather than hiding Blog.
            Demo is the one link that waits for sm: a fifth item overflows a
            360px or a 390px phone, and there the hero's link to the sample
            workspace sits just below this header.
          */}
          <TrackedLink
            href="/demo"
            track={{ event: "Sample Workspace Click", props: { location: "header" } }}
            className={`hidden sm:inline ${navLinkClass}`}
          >
            Demo
          </TrackedLink>
          <Link href="/blog" className={navLinkClass}>
            Blog
          </Link>
          <TrackedLink
            href="/login"
            track={{ event: "Sign In Click", props: { location: "header" } }}
            className={navLinkClass}
          >
            Sign in
          </TrackedLink>
          <WaitlistLink
            className={buttonVariants({ size: "sm", className: "h-auto px-3 py-2.5 text-xs sm:px-4 sm:text-sm" })}
          >
            {/*
              "the" is dropped below `sm` so all four items fit a 360px phone
              without hiding any of them. The label still reads as the same
              offer; the alternative was letting the CTA overflow the viewport.
            */}
            {/* One flex item, so the button's gap doesn't split the words. */}
            <span>Join <span className="hidden sm:inline">the </span>waitlist</span>
          </WaitlistLink>
        </div>
      </div>
    </header>
  )
}
