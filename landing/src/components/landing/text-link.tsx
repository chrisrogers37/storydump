import Link from "next/link"
import type { ReactNode } from "react"
import { cn } from "@/lib/utils"

/** The marketing pages' inline link: ink, underlined, orange on hover. */
export const linkClass =
  "font-medium text-ink underline decoration-ink/30 underline-offset-4 hover:text-tap-ink hover:decoration-tap-ink"

export function TextLink({
  href,
  className,
  children,
}: {
  href: string
  className?: string
  children: ReactNode
}) {
  return (
    <Link href={href} className={cn(linkClass, className)}>
      {children}
    </Link>
  )
}
