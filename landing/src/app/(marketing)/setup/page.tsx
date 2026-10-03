import type { Metadata } from "next"
import { pageMetadata } from "@/lib/seo"
import Link from "next/link"
import { ArrowRight } from "lucide-react"
import { Checklist } from "@/components/setup/checklist"
import { StepCard } from "@/components/setup/step-card"
import { UiTerm } from "@/components/setup/ui-term"

export const metadata: Metadata = pageMetadata({
  title: "Getting Started",
  description:
    "What to have ready for Storydump: a Google account, an Instagram Business or Creator account and a Drive folder of media. Telegram is optional.",
  path: "/setup",
})

const prerequisites = [
  { label: "A Google account, to sign in and connect Google Drive" },
  { label: "An Instagram Business or Creator account" },
  { label: "A Google Drive folder of photos and videos" },
  { label: "Optional: a Telegram group for your team, to approve Stories from your phone" },
]

export default function SetupOverview() {
  return (
    <div>
      <h1 className="font-display text-4xl font-extrabold tracking-[-0.03em] text-ink">
        Getting Started with Storydump
      </h1>
      <p className="mt-4 text-lg text-muted-foreground">
        Everything happens on the web. Get these ready, connect them in
        Settings, and Storydump starts bringing you Stories inside your
        posting window.
      </p>

      <div className="mt-8">
        <h2 className="text-xl font-semibold">What You&apos;ll Need</h2>
        <div className="mt-4">
          <Checklist items={prerequisites} />
        </div>
      </div>

      <div className="mt-10">
        <h2 className="text-xl font-semibold">Setup, step by step</h2>
        <p className="mt-2 text-muted-foreground">
          Sign in with Google, name your workspace, then open{" "}
          <UiTerm>Settings</UiTerm>:
        </p>
        <div className="mt-6 space-y-8">
          <StepCard number={1} title="Connect Instagram">
            <p>
              In <UiTerm>Accounts</UiTerm>, tap <UiTerm>Connect Instagram</UiTerm>{" "}
              and log in to the account your Stories go to. It has to be a
              Business or Creator account.{" "}
              <Link href="/setup/instagram" className="underline underline-offset-4 hover:text-foreground">
                How to switch
              </Link>
            </p>
          </StepCard>
          <StepCard number={2} title="Connect Google Drive and pick folders">
            <p>
              In <UiTerm>Integrations</UiTerm>, tap{" "}
              <UiTerm>Connect Google Drive</UiTerm> (read-only), then{" "}
              <UiTerm>Add folder</UiTerm> for each folder to post from.{" "}
              <Link href="/setup/media-organize" className="underline underline-offset-4 hover:text-foreground">
                How to organize your folders
              </Link>
            </p>
          </StepCard>
          <StepCard number={3} title="Set your schedule and mix">
            <p>
              In <UiTerm>General</UiTerm>, set <UiTerm>Posts per day</UiTerm>, the{" "}
              <UiTerm>Start hour</UiTerm> and <UiTerm>End hour</UiTerm> of your
              posting window and your <UiTerm>Time zone</UiTerm>, then tap{" "}
              <UiTerm>Save Schedule</UiTerm>. The <UiTerm>Posting mix</UiTerm> card
              sets how much each folder posts.
            </p>
          </StepCard>
          <StepCard number={4} title="Link Telegram (optional)">
            <p>
              Each Story waits in your <UiTerm>Queue</UiTerm> on the web. Link a
              Telegram group and it also arrives there as a card your whole
              team can tap.{" "}
              <Link href="/setup/connect" className="underline underline-offset-4 hover:text-foreground">
                How to link Telegram
              </Link>
            </p>
          </StepCard>
        </div>
      </div>

      <div className="mt-10">
        <h2 className="text-xl font-semibold">Ready? Let&apos;s go.</h2>
        <p className="mt-2 text-muted-foreground">
          The guides go in order, starting with your Instagram account.
        </p>
        <Link
          href="/setup/instagram"
          className="mt-4 inline-flex items-center gap-2 rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground transition-colors hover:bg-primary/90"
        >
          Start with Instagram
          <ArrowRight className="h-4 w-4" />
        </Link>
      </div>
    </div>
  )
}
