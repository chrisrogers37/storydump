import Link from "next/link"
import { siteConfig } from "@/config/site"
import { BrandMark } from "./brand-mark"

export function Header() {
  return (
    <header className="sticky top-0 z-50 w-full border-b border-ink/10 bg-white/85 backdrop-blur-sm">
      <div className="mx-auto flex h-16 max-w-6xl items-center justify-between px-4">
        <Link
          href="/"
          className="flex shrink-0 items-center gap-2 pr-2 font-display text-lg font-extrabold tracking-[-0.03em] text-ink sm:pr-0 sm:text-xl"
        >
          <BrandMark />
          {siteConfig.name}
        </Link>
        <div className="flex items-center gap-2 sm:gap-4">
          {/*
            Nothing is hidden at a breakpoint here, deliberately. At 390px the
            wordmark collided with Blog and "Sign in" wrapped onto two lines
            (#1090 A1); tightened until all four fit rather than hiding Blog
            on a phone.
          */}
          <Link
            href="/blog"
            className="whitespace-nowrap py-2.5 text-sm text-muted-foreground transition-colors hover:text-foreground"
          >
            Blog
          </Link>
          <Link
            href="/login"
            className="whitespace-nowrap py-2.5 text-sm text-muted-foreground transition-colors hover:text-foreground"
          >
            Sign in
          </Link>
          <Link
            href="/#waitlist"
            className="whitespace-nowrap rounded-full bg-ink px-3 py-2.5 text-xs font-semibold text-white transition-colors hover:bg-ink/85 sm:px-4 sm:text-sm"
          >
            {/*
              "the" is dropped below `sm` so all four items fit a 360px phone
              without hiding any of them. The label still reads as the same
              offer; the alternative was letting the CTA overflow the viewport.
            */}
            Join <span className="hidden sm:inline">the </span>waitlist
          </Link>
        </div>
      </div>
    </header>
  )
}
