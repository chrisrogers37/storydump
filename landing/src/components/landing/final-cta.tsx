import Link from "next/link"
import { WaitlistForm } from "@/components/landing/waitlist-form"

export function FinalCTA() {
  return (
    <section aria-labelledby="closing-heading" className="bg-tap py-16 md:py-24">
      <div className="mx-auto max-w-4xl px-4 text-center">
        <h2
          id="closing-heading"
          className="section-title"
        >
          Tomorrow’s Story is already in your library.
        </h2>
        <p className="mt-5 font-medium text-ink">
          Free during beta · No credit card required.
        </p>
        <div className="mx-auto mt-8 max-w-lg">
          <WaitlistForm variant="footer" />
        </div>
        <p className="mt-6 text-sm text-ink">
          Already using Storydump?{" "}
          <Link href="/login" className="font-medium underline underline-offset-4">
            Sign in
          </Link>
        </p>
      </div>
    </section>
  )
}
