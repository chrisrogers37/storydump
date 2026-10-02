import Link from "next/link"
import { siteConfig } from "@/config/site"
import { pathForUseCase, useCases } from "@/lib/use-cases"

const linkClass = "underline underline-offset-4 hover:text-foreground"

const siteLinks = [
  { href: "/setup", label: "Setup guide" },
  { href: "/blog", label: "Blog" },
  { href: `mailto:${siteConfig.contact.email}`, label: "Contact" },
  { href: "/login", label: "Sign in" },
  { href: "/privacy", label: "Privacy" },
  { href: "/terms", label: "Terms" },
]

/** Links joined by middots, the way the footer has always read. */
function Dotted({ links }: { links: { href: string; label: string }[] }) {
  return links.map(({ href, label }, i) => (
    <span key={href}>
      {i > 0 && " · "}
      {href.startsWith("mailto:") ? (
        <a href={href} className={linkClass}>
          {label}
        </a>
      ) : (
        <Link href={href} className={linkClass}>
          {label}
        </Link>
      )}
    </span>
  ))
}

export function Footer() {
  return (
    <footer className="border-t border-ink/10 py-8">
      <div className="mx-auto flex max-w-6xl flex-col items-center justify-between gap-4 px-4 text-center text-sm text-muted-foreground sm:flex-row sm:items-start sm:text-left">
        <p>&copy; {new Date().getFullYear()} {siteConfig.name}</p>
        <div className="space-y-2 sm:text-right">
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
