import { cn } from "@/lib/utils"

const SKELETON = "animate-pulse rounded-md bg-primary/10"

function Skeleton({ className, ...props }: React.ComponentProps<"div">) {
  return (
    <div
      data-slot="skeleton"
      className={cn(SKELETON, className)}
      aria-hidden="true"
      {...props}
    />
  )
}

/** A Skeleton for a word inside a line of text: a span, since a div may not
 *  sit in a <p>, and no taller than the text, so the line keeps its height. */
function TextSkeleton({ className, ...props }: React.ComponentProps<"span">) {
  return (
    <span
      data-slot="skeleton"
      className={cn(SKELETON, "inline-block h-[1em] align-middle", className)}
      aria-hidden="true"
      {...props}
    />
  )
}

export { Skeleton, TextSkeleton }
