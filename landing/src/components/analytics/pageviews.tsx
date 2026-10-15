"use client"

import { usePathname } from "next/navigation"
import { useEffect } from "react"
import { capture } from "@/lib/posthog"

/** Sends a pageview on the first render and on every route change after it. */
export function Pageviews() {
  const pathname = usePathname()
  useEffect(() => {
    capture("$pageview")
  }, [pathname])
  return null
}
