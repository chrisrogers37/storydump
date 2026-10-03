export const UTM_KEYS = ["utm_source", "utm_medium", "utm_campaign"] as const

type UtmKey = (typeof UTM_KEYS)[number]
type Variant = "hero" | "footer"

/** Where a "Join the waitlist" link that isn't the form itself sits. */
export const CTA_LOCATIONS = ["header", "blog_post", "use_case", "login"] as const
/** Where a "Sign in" link sits. */
export const SIGN_IN_LOCATIONS = ["header", "hero", "closing", "footer"] as const
/** The home page demo's buttons. */
export const DEMO_ACTIONS = [
  "post_now",
  "posted_myself",
  "skip",
  "reject",
  "open_instagram",
  "another",
] as const
export type DemoAction = (typeof DEMO_ACTIONS)[number]

/**
 * Every event the site sends, with its properties. Each value comes from a
 * fixed list in the code, with two exceptions: the FAQ's question is
 * `faqs.ts`'s own text, and the UTM tags are copied from the landing URL.
 * Nothing a visitor types is sent. Plausible records the page itself, so no
 * event carries one.
 */
export interface Events {
  "Waitlist Signup": { variant: Variant } & Partial<Record<UtmKey, string>>
  "Waitlist Error": {
    reason: "invalid_email" | "server_error" | "network_error"
    variant: Variant
  }
  "Waitlist Start": { variant: Variant }
  "FAQ Expanded": { question: string }
  "CTA Click": { location: (typeof CTA_LOCATIONS)[number] }
  "Sign In Click": { location: (typeof SIGN_IN_LOCATIONS)[number] }
  "Demo Tap": { action: DemoAction }
}

export type EventName = keyof Events

/** One event with its properties, for components that take an event to send. */
export type Tracked = { [E in EventName]: { event: E; props: Events[E] } }[EventName]

export function trackEvent<E extends EventName>(name: E, props: Events[E]) {
  if (typeof window === "undefined") return
  // An invitation link's path is its token, and every event carries the URL.
  // The join route loads no sender (join-fires-no-analytics-event-contract),
  // but the root 404 page can render under /join/…, so refuse here as well.
  if (window.location.pathname.startsWith("/join/")) return
  // Plausible's own queue stub: an event fired before its script has loaded
  // (a tap on a slow connection) waits in `plausible.q`, which the script
  // sends when it arrives, rather than being dropped.
  window.plausible ??= Object.assign(
    (...args: PlausibleArgs) => (window.plausible!.q ??= []).push(args),
    { q: [] as PlausibleArgs[] },
  )
  window.plausible(name, { props })
}

type PlausibleArgs = [event: string, options?: { props?: Record<string, unknown> }]

declare global {
  interface Window {
    plausible?: ((...args: PlausibleArgs) => void) & { q?: PlausibleArgs[] }
  }
}
