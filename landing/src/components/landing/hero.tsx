import Link from "next/link"
import { WaitlistForm } from "@/components/landing/waitlist-form"
import { TapDemo } from "@/components/landing/tap-demo"

export function Hero() {
  return (
    <section aria-labelledby="hero-heading" className="py-14 md:py-20">
      <div className="mx-auto grid max-w-6xl items-center gap-12 px-4 md:grid-cols-[1.1fr_1fr] md:gap-10">
        <div>
          <p className="flex items-center gap-2 kicker text-ink">
            <span aria-hidden="true" className="size-2 rounded-full bg-tap" />
            Instagram Stories for small teams
          </p>
          <h1
            id="hero-heading"
            className="mt-5 font-display text-6xl font-extrabold leading-[0.9] tracking-[-0.04em] text-ink sm:text-7xl lg:text-[6.5rem]"
          >
            Instagram <br />
            Stories, <br />
            on tap<span className="text-tap">.</span>
          </h1>
          <p className="mt-7 max-w-xl text-lg leading-relaxed text-ink/80">
            Storydump picks today’s Story from your own photos and videos, gets
            it ready and brings it to your team right on time. Tap{" "}
            <strong className="font-semibold text-ink">Post now</strong> and
            it’s up.
          </p>
          <div className="mt-8 max-w-xl">
            <WaitlistForm variant="hero" />
          </div>
          <p className="mt-4 text-sm text-ink/70">
            Free during beta · No credit card required ·{" "}
            <span className="whitespace-nowrap">
              Already using Storydump?{" "}
              <Link
                href="/login"
                className="font-medium text-ink underline underline-offset-4 hover:text-tap-ink"
              >
                Sign in
              </Link>
            </span>
          </p>
        </div>
        <TapDemo />
      </div>
    </section>
  )
}
