import type { CaptureResult, PostHog, PostHogConfig } from "posthog-js/dist/module.slim.no-external"
import { POSTHOG_HOST } from "@/lib/posthog-host"
import { utmFrom } from "@/lib/utm"

/**
 * The site's analytics: PostHog in cookieless mode, sending pageviews (here)
 * and the custom events in `analytics.ts`, and nothing else.
 *
 * Cookieless: nothing is kept in the visitor's browser (the library only
 * checks that storage works, writing a test key and removing it at once).
 * PostHog counts a visitor by a hash of the site, the visitor's address and
 * browser and a salt it replaces daily, computed on its servers. The address is dropped before
 * the event is stored, so no event records it or a location derived from it.
 * The project needs "Cookieless server hash mode" turned on, or PostHog drops
 * every event at ingestion.
 *
 * The library is posthog-js's slim build, which bundles none of the optional
 * features (autocapture, session replay, heatmaps, surveys, feature flags,
 * exception capture). It is downloaded from this site once the browser is
 * idle after the first event, which calls queue for until then;
 * `disable_external_dependency_loading` stops it fetching further scripts, so
 * the only third-party origin the page talks to is the ingestion host
 * (`posthog-host.ts`).
 */

const KEY = process.env.NEXT_PUBLIC_POSTHOG_KEY

/** An invite link's path is its token, a bearer credential; nothing under it is sent. */
function isInvitePath(pathname: string): boolean {
  return pathname.startsWith("/join/")
}

/** An address, or null for one that does not parse (`$direct`, say). */
function parse(value: unknown): URL | null {
  if (typeof value !== "string") return null
  try {
    return new URL(value)
  } catch {
    return null
  }
}

/**
 * Strips an event to what the site means to send: the page's address without
 * its query string, the UTM tags as properties, the referrer's site only, no
 * page title. An event on an invite page is dropped whole, and an invite page
 * is never named as the previous page.
 */
export function redact(event: CaptureResult | null): CaptureResult | null {
  if (!event) return null
  const props = event.properties
  const page = parse(props.$current_url)
  if (page) {
    if (isInvitePath(page.pathname)) return null
    for (const [key, value] of Object.entries(utmFrom(page.searchParams))) props[key] ??= value
    props.$current_url = page.origin + page.pathname
  }
  const referrer = parse(props.$referrer)
  if (referrer) props.$referrer = referrer.origin
  if (typeof props.$prev_pageview_pathname === "string" && isInvitePath(props.$prev_pageview_pathname)) {
    delete props.$prev_pageview_pathname
  }
  delete props.title
  delete event.$set
  delete event.$set_once
  return event
}

const CONFIG: Partial<PostHogConfig> = {
  api_host: POSTHOG_HOST,
  cookieless_mode: "always",
  persistence: "memory",
  person_profiles: "never",
  // A link carrying ?__posthog_debug=true would otherwise store a debug flag
  // in the visitor's localStorage.
  debug: false,
  // Pageviews are sent by `components/analytics/pageviews.tsx`, on each route
  // change; with this off the library sends no pageleave either.
  capture_pageview: false,
  autocapture: false,
  disable_session_recording: true,
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

let library: Promise<PostHog> | undefined

/** Downloads and starts the library once, when the browser is next idle. */
function load(key: string): Promise<PostHog> {
  library ??= new Promise<void>((resolve) =>
    "requestIdleCallback" in window ? requestIdleCallback(() => resolve()) : setTimeout(resolve, 1),
  )
    .then(() => import("posthog-js/dist/module.slim.no-external"))
    .then(({ default: posthog }) => {
      posthog.init(key, CONFIG)
      return posthog
    })
  return library
}

/**
 * Sends one event; with no project key, or on an invite page, nothing. The
 * time is taken now, so an event fired while the library downloads keeps the
 * moment it happened. Calls made meanwhile are sent in order once it arrives;
 * if it fails to load they are dropped.
 */
export function capture(name: string, properties?: Record<string, unknown>) {
  if (!KEY || typeof window === "undefined" || isInvitePath(window.location.pathname)) return
  const timestamp = new Date()
  // The page the event happened on, not the one the visitor is on when the
  // library arrives.
  const page = { $current_url: window.location.href, $pathname: window.location.pathname }
  load(KEY).then(
    (posthog) => {
      // The library notes the current page as it sends (the next pageview's
      // previous page): never let it note an invite page.
      if (isInvitePath(window.location.pathname)) return
      posthog.capture(name, { ...properties, ...page }, { timestamp })
    },
    () => {},
  )
}
