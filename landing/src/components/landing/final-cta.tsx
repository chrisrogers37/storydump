import { TextLink } from "@/components/landing/text-link"
import { WaitlistForm } from "@/components/landing/waitlist-form"
import { cn } from "@/lib/utils"

/** The closing signup: the home page's, and each use-case page's with its own heading. */
export function FinalCTA({
  heading = "Tomorrow’s Story is already in your library.",
  headingClassName = "md:text-[5.75rem]",
}: {
  heading?: string
  headingClassName?: string
}) {
  return (
    <section aria-labelledby="closing-heading" className="bg-tap py-16 md:py-24">
      <div className="mx-auto max-w-4xl px-4 text-center">
        <h2
          id="closing-heading"
          className={cn("section-title", headingClassName)}
        >
          {heading}
        </h2>
        <p className="mt-5 font-medium text-ink">
          Free during beta · No credit card required.
        </p>
        <div className="mx-auto mt-8 max-w-lg">
          <WaitlistForm variant="footer" />
        </div>
        <p className="mt-6 text-sm text-ink">
          Already using Storydump?{" "}
          <TextLink href="/login">Sign in</TextLink>
        </p>
      </div>
    </section>
  )
}
