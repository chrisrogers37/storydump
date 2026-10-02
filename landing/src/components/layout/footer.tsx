import { Fragment } from "react"
import { MaybeTrackedLink } from "@/components/analytics/maybe-tracked-link"
import { siteConfig } from "@/config/site"
import type { Tracked } from "@/lib/analytics"
import { pathForUseCase, useCases } from "@/lib/use-cases"

const linkClass = "underline underline-offset-4 hover:text-foreground"

type FooterLink = { href: string; label: string; track?: Tracked }

const siteLinks: FooterLink[] = [
  { href: "/setup", label: "Setup guide" },
  { href: "/blog", label: "Blog" },
  { href: `mailto:${siteConfig.contact.email}`, label: "Contact" },
  { href: "/login", label: "Sign in", track: { event: "Sign In Click", props: { location: "footer" } } },
  { href: "/privacy", label: "Privacy" },
  { href: "/terms", label: "Terms" },
]

/**
 * Links joined by middots, the way the footer has always read. Each link
 * keeps its words together; the separator sits outside so a row can wrap.
 */
function Dotted({ links }: { links: FooterLink[] }) {
  return links.map(({ href, label, track }, i) => (
    <Fragment key={href}>
      {i > 0 && " · "}
      <MaybeTrackedLink href={href} track={track} className={`whitespace-nowrap ${linkClass}`}>
        {label}
      </MaybeTrackedLink>
    </Fragment>
  ))
}

export function Footer() {
  return (
    <footer className="border-t border-ink/10 py-8">
      <div className="mx-auto flex max-w-6xl flex-col items-center justify-between gap-4 px-4 text-center text-sm text-muted-foreground sm:flex-row sm:items-start sm:text-left">
        <p>&copy; {new Date().getFullYear()} {siteConfig.name}</p>
        <div className="space-y-3 leading-7 sm:text-right">
          <nav aria-label="Footer">
            Built by{" "}
            <a
              href={siteConfig.contact.portfolio}
              target="_blank"
              rel="noopener noreferrer"
              className={linkClass}
            >
              Chris
            </a>
            {" · "}
            <Dotted links={siteLinks} />
          </nav>
          <nav aria-label="Use cases">
            Use cases:{" "}
            <Dotted
              links={useCases.map(({ slug, navLabel }) => ({ href: pathForUseCase(slug), label: navLabel }))}
            />
          </nav>
        </div>
      </div>
    </footer>
  )
}
