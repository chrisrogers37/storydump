import type { Tracked } from "@/lib/analytics"

/** A post's "join the waitlist" link, counted with the closing button. */
export const blogCta: Tracked = { event: "CTA Click", props: { location: "blog_post" } }
