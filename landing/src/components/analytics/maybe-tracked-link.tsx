import Link from "next/link"
import type { ComponentProps } from "react"
import type { Tracked } from "@/lib/analytics"
import { TrackedLink } from "./tracked-link"

/**
 * A `next/link` that sends `track` when it's given one. An untracked link
 * stays a plain server-rendered `Link`, so only tracked ones cost client code.
 */
export function MaybeTrackedLink({ track, ...props }: ComponentProps<typeof Link> & { track?: Tracked }) {
  return track ? <TrackedLink track={track} {...props} /> : <Link {...props} />
}
