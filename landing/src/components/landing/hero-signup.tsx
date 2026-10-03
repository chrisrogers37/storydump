import { TextLink } from "@/components/landing/text-link"
import { WaitlistForm } from "@/components/landing/waitlist-form"

/** The top-of-page signup: the form, the locked price line and Sign in. */
export function HeroSignup() {
  return (
    <>
      <div className="mt-8 max-w-xl">
        <WaitlistForm variant="hero" />
      </div>
      <p className="mt-4 text-sm text-ink/70">
        Free during beta · No credit card required{" "}
        <span className="whitespace-nowrap">
          · Already using Storydump? <TextLink href="/login" track={{ event: "Sign In Click", props: { location: "hero" } }}>
            Sign in
          </TextLink>
        </span>
      </p>
    </>
  )
}
