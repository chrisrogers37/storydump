"use client"

import { usePathname } from "next/navigation"
import type { ReactNode } from "react"
import { TrackedLink } from "@/components/analytics/tracked-link"

// Pages that carry their own hero form (id "waitlist"): the header's button
// scrolls to it there, and goes to the home page's everywhere else.
const hasForm = (pathname: string) => pathname === "/" || pathname.startsWith("/use-cases/")

export function WaitlistLink({ className, children }: { className?: string; children: ReactNode }) {
  const pathname = usePathname()
  return (
    <TrackedLink
      href={hasForm(pathname) ? "#waitlist" : "/#waitlist"}
      track={{ event: "CTA Click", props: { location: "header" } }}
      className={className}
    >
      {children}
    </TrackedLink>
  )
}
