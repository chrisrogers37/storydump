import { Lock, Instagram, KeyRound } from "lucide-react"
import { WaitlistForm } from "@/components/landing/waitlist-form"

const trustBadges = [
  { icon: Lock, text: "Your content stays in Google Drive" },
  { icon: Instagram, text: "Official Instagram API" },
  { icon: KeyRound, text: "No password required" },
]

export function Hero() {
  return (
    <section aria-label="Hero" className="py-20 md:py-32">
      <div className="mx-auto max-w-5xl px-4 text-center">
        <h1 className="text-4xl font-bold tracking-tight sm:text-5xl md:text-6xl">
          Instagram Stories on Autopilot
        </h1>
        {/*
          "no dashboard logins" was here and is FALSE. Every workspace has web
          access by construction, and the dashboard is where the queue,
          settings, Drive connect and sync now live — so the first sentence a
          visitor read asserted the opposite of the product (#1090 A1).

          The differentiator was never the absence of a dashboard; it is
          one-tap approval from a chat you already have open, and that is
          untouched. Telegram moves from "instead of" to "as well as", which is
          what is true: a workspace with no Telegram binding still reaches
          `awaiting_approval` and stays actionable on the web.
        */}
        <p className="mx-auto mt-6 max-w-2xl text-lg text-muted-foreground">
          Your content library, posted to Stories on a schedule you set.
          Approve each post with one tap — no manual uploads, no missed days.
        </p>
        <p className="mt-3 text-sm font-medium text-foreground/80">
          The only Story tool that lives where you already are — approve from
          Telegram, or from your dashboard.
        </p>
        <div className="mt-10">
          <WaitlistForm variant="hero" />
        </div>
        <div className="mt-4 flex flex-wrap items-center justify-center gap-x-5 gap-y-2">
          {trustBadges.map((badge) => (
            <span
              key={badge.text}
              className="flex items-center gap-1.5 text-xs text-muted-foreground"
            >
              <badge.icon className="h-3.5 w-3.5" />
              {badge.text}
            </span>
          ))}
        </div>
        <p className="mt-2 text-sm text-muted-foreground">
          Free during beta &middot; No credit card required &middot; 2,400+
          stories posted and counting
        </p>
      </div>
    </section>
  )
}
