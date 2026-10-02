import type { ReactNode } from "react"
import { MaybeTrackedLink } from "@/components/analytics/maybe-tracked-link"
import type { Tracked } from "@/lib/analytics"
import { cn } from "@/lib/utils"

/** The marketing pages' inline link: ink, underlined, orange on hover. */
export const linkClass =
  "font-medium text-ink underline decoration-ink/30 underline-offset-4 hover:text-tap-ink hover:decoration-tap-ink"

export function TextLink({
  href,
  className,
  track,
  children,
}: {
  href: string
  className?: string
  /** An event to send on click. */
  track?: Tracked
  children: ReactNode
}) {
  return (
    <MaybeTrackedLink href={href} className={cn(linkClass, className)} track={track}>
      {children}
    </MaybeTrackedLink>
  )
}
