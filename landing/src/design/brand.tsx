import { siteConfig } from "@/config/site"
import { cn } from "@/lib/utils"

/** The wordmark's symbol: an orange ring around an ink dot, as in the site icon. */
function BrandMark({ className = "size-5" }: { className?: string }) {
  return (
    <span
      aria-hidden="true"
      className={cn("flex shrink-0 items-center justify-center rounded-full border-[3px] border-tap", className)}
    >
      <span className="size-[30%] rounded-full bg-ink" />
    </span>
  )
}

/**
 * The mark and the name in the display face. The caller sets the size and
 * wraps it in whatever it is (a link, the sign-in H1).
 */
export function Wordmark({ className, markClassName }: { className?: string; markClassName?: string }) {
  return (
    <span className={cn("page-title flex items-center gap-2 text-ink", className)}>
      <BrandMark className={markClassName} />
      {siteConfig.name}
    </span>
  )
}
