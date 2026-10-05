"use client"

import { usePathname } from "next/navigation"
import { useEffect } from "react"
import { capturePageview } from "@/lib/posthog"

/** Sends a pageview on the first render and on every route change after it. */
export function Pageviews() {
  const pathname = usePathname()
  useEffect(() => {
    capturePageview()
  }, [pathname])
  return null
}
