"use client"

import Link from "next/link"
import type { ComponentProps } from "react"
import { trackEvent, type Tracked } from "@/lib/analytics"

/**
 * A `next/link` that sends one analytics event when clicked. It lets a server
 * component (the header, the footer, a blog post) track a link while only the
 * link itself becomes a client island.
 */
export function TrackedLink({
  track,
  onClick,
  ...props
}: ComponentProps<typeof Link> & { track: Tracked }) {
  return (
    <Link
      {...props}
      onClick={(e) => {
        trackEvent(track.event, track.props)
        onClick?.(e)
      }}
    />
  )
}
