import type { CaptureResult, PostHog, PostHogConfig } from "posthog-js/dist/module.slim.no-external"
import { POSTHOG_HOST } from "@/lib/posthog-host"
import { UTM_KEYS } from "@/lib/utm"

/**
 * The site's analytics: PostHog in cookieless mode, sending pageviews (here)
 * and the custom events in `analytics.ts`, and nothing else.
 *
 * Cookieless: nothing is written to the visitor's browser. PostHog counts a
 * visitor by a hash of the site, the visitor's address and browser and a salt
 * it replaces daily, computed on its servers. The address is dropped before
 * the event is stored, so no event records it or a location derived from it.
 * The project needs "Cookieless server hash mode" turned on, or PostHog drops
 * every event at ingestion.
 *
 * The library is posthog-js's slim build, which bundles none of the optional
 * features (autocapture, session replay, heatmaps, surveys, feature flags,
 * exception capture). It is downloaded from this site, after the page has
 * hydrated, the first time something is sent; `disable_external_dependency_loading`
 * stops it fetching further scripts, so the only third-party origin the page
 * talks to is the ingestion host (`posthog-host.ts`).
 */

const KEY = process.env.NEXT_PUBLIC_POSTHOG_KEY

/** An invite link's path is its token, a bearer credential; nothing under it is sent. */
export function isInvitePath(pathname: string): boolean {
  return pathname.startsWith("/join/")
}

/**
 * Strips an event to what the site means to send: the page's address without
 * its query string, the UTM tags as properties, the referrer's site only.
 * An event on an invite page is dropped whole.
 */
export function redact(event: CaptureResult | null): CaptureResult | null {
  if (!event) return null
  const props = event.properties
  if (typeof props.$current_url === "string") {
    const url = new URL(props.$current_url)
    if (isInvitePath(url.pathname)) return null
    for (const key of UTM_KEYS) {
      const value = url.searchParams.get(key)
      if (value) props[key] ??= value
    }
    props.$current_url = url.origin + url.pathname
  }
  if (typeof props.$referrer === "string" && URL.canParse(props.$referrer)) {
    props.$referrer = new URL(props.$referrer).origin
  }
  delete event.$set
  delete event.$set_once
  return event
}

const CONFIG: Partial<PostHogConfig> = {
  api_host: POSTHOG_HOST,
  cookieless_mode: "always",
  person_profiles: "never",
  // Pageviews are sent by `capturePageview`, on each route change.
  capture_pageview: false,
  capture_pageleave: false,
  autocapture: false,
  rageclick: false,
  capture_dead_clicks: false,
  capture_heatmaps: false,
  capture_exceptions: false,
  capture_performance: false,
  disable_session_recording: true,
  disable_surveys: true,
  disable_product_tours: true,
  disable_conversations: true,
  disable_web_experiments: true,
  // No feature-flag or remote-config call: the project's settings cannot turn
  // on anything this file leaves off.
  advanced_disable_flags: true,
  disable_external_dependency_loading: true,
  // `redact` adds the UTM tags; the library's own list also takes ad click
  // ids (gclid, fbclid, …), which the site does not want.
  save_campaign_params: false,
  property_denylist: [
    // The search terms in a search engine's referrer.
    "ph_keyword",
    "$screen_height",
    "$screen_width",
    "$viewport_height",
    "$viewport_width",
    "$timezone",
    "$timezone_offset",
    "$browser_language",
    "$browser_language_prefix",
  ],
  before_send: redact,
}

type Call = (posthog: PostHog) => void

let client: PostHog | undefined
/** Calls made while the library downloads; undefined until the first call, null if it failed. */
let pending: Call[] | null | undefined

/** Runs `call` once the library has loaded; with no project key, never. */
function withPostHog(call: Call) {
  if (!KEY || typeof window === "undefined") return
  if (client) return call(client)
  if (pending === null) return
  if (pending === undefined) {
    const queue: Call[] = (pending = [])
    import("posthog-js/dist/module.slim.no-external").then(
      ({ default: posthog }) => {
        posthog.init(KEY, CONFIG)
        client = posthog
        for (const queued of queue.splice(0)) queued(posthog)
      },
      () => {
        pending = null
      },
    )
  }
  pending.push(call)
}

/**
 * Sends one event. The time is taken now, so an event fired while the library
 * downloads keeps the moment it happened.
 */
export function capture(name: string, properties?: Record<string, unknown>) {
  if (typeof window === "undefined" || isInvitePath(window.location.pathname)) return
  const timestamp = new Date()
  withPostHog((posthog) => posthog.capture(name, properties, { timestamp }))
}

/** Sends a pageview for the page the visitor is on. */
export function capturePageview() {
  capture("$pageview")
}
