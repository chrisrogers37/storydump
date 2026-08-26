import Link from "next/link"
import { siteConfig } from "@/config/site"

export function Header() {
  return (
    <header className="sticky top-0 z-50 w-full border-b bg-background/80 backdrop-blur-sm">
      <div className="mx-auto flex h-14 max-w-5xl items-center justify-between px-4">
        <Link href="/" className="shrink-0 pr-2 text-base font-semibold tracking-tight sm:pr-0 sm:text-lg">
          {siteConfig.name}
        </Link>
        <div className="flex items-center gap-2 sm:gap-4">
          {/*
            Nothing is hidden at a breakpoint here, deliberately. At 390px the
            wordmark collided with Blog and "Sign in" wrapped onto two lines
            (#1090 A1); the reflex fix is `hidden sm:inline` on Blog, but Blog
            is NOT in the footer, so that would make it unreachable on a phone
            — trading one discoverability defect for another, on the issue
            about discoverability. Tightened instead until all four fit.
          */}
          <Link
            href="/blog"
            className="whitespace-nowrap text-sm text-muted-foreground transition-colors hover:text-foreground"
          >
            Blog
          </Link>
          <Link
            href="/login"
            className="whitespace-nowrap text-sm text-muted-foreground transition-colors hover:text-foreground"
          >
            Sign in
          </Link>
          <a
            href="#waitlist"
            className="whitespace-nowrap rounded-md bg-primary px-2.5 py-2 text-xs font-medium text-primary-foreground transition-colors hover:bg-primary/90 sm:px-4 sm:text-sm"
          >
            {/*
              "Get" is dropped below `sm` so all four items fit a 360px phone
              without hiding any of them. The label still reads as the same
              offer; the alternative was letting the CTA overflow the viewport,
              which is what tightening alone left at 320-360.
            */}
            <span className="hidden sm:inline">Get </span>Early Access
          </a>
        </div>
      </div>
    </header>
  )
}
