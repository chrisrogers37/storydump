import type { Tracked } from "@/lib/analytics"

/** A post's "join the waitlist" link, counted with the closing button. */
export const blogCta: Tracked = { event: "CTA Click", props: { location: "blog_post" } }

/** A post's link to the sample workspace. */
export const blogDemo: Tracked = { event: "Sample Workspace Click", props: { location: "blog_post" } }
