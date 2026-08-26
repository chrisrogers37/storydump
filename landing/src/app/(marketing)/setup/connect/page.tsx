import type { Metadata } from "next"
import Link from "next/link"
import { ArrowLeft, CheckCircle2 } from "lucide-react"
import { StepCard } from "@/components/setup/step-card"
import { Callout } from "@/components/setup/callout"
import { siteConfig } from "@/config/site"

export const metadata: Metadata = {
  title: "Connect to Telegram — Storydump",
  robots: { index: false, follow: false },
}

export default function ConnectTelegram() {
  return (
    <div>
      <h1 className="text-3xl font-bold tracking-tight">
        Connect to Telegram
      </h1>
      <p className="mt-4 text-lg text-muted-foreground">
        Optional — link the Storydump Telegram bot to approve Stories from your
        phone. You can do everything on the web instead.
      </p>

      <div className="mt-10 space-y-10">
        <StepCard number={1} title="Install Telegram (if needed)">
          <p>
            If you don&apos;t already have Telegram, download it for your
            platform:
          </p>
          <ul className="mt-2 list-inside list-disc space-y-1">
            <li>
              <a
                href="https://apps.apple.com/app/telegram-messenger/id686449807"
                target="_blank"
                rel="noopener noreferrer"
                className="underline underline-offset-4 hover:text-foreground"
              >
                iOS (App Store)
              </a>
            </li>
            <li>
              <a
                href="https://play.google.com/store/apps/details?id=org.telegram.messenger"
                target="_blank"
                rel="noopener noreferrer"
                className="underline underline-offset-4 hover:text-foreground"
              >
                Android (Google Play)
              </a>
            </li>
            <li>
              <a
                href="https://desktop.telegram.org/"
                target="_blank"
                rel="noopener noreferrer"
                className="underline underline-offset-4 hover:text-foreground"
              >
                Desktop (Windows, macOS, Linux)
              </a>
            </li>
          </ul>
          <p className="mt-2">Already have Telegram? Skip to Step 2.</p>
        </StepCard>

        <StepCard number={2} title="Start the bot">
          <p>
            Your invite link will be shared directly with you via email when your
            waitlist spot opens up. Once you have it:
          </p>
          <ol className="mt-2 list-inside list-decimal space-y-1">
            <li>Tap the link to open it in Telegram</li>
            <li>
              Tap{" "}
              <span className="font-medium text-foreground">
                &quot;Start&quot;
              </span>{" "}
              to begin
            </li>
          </ol>
          <Callout type="info" className="mt-3">
            The bot link is shared privately with accepted waitlist users. If you
            haven&apos;t received yours yet, check your email or reach out to us.
          </Callout>
        </StepCard>

        <StepCard number={3} title="Set up your workspace on the web">
          <p>
            Setup happens in the dashboard, not in the chat. Sign in and open{" "}
            <span className="font-medium text-foreground">Settings</span>:
          </p>
          <ol className="mt-2 list-inside list-decimal space-y-1">
            <li>
              <span className="font-medium text-foreground">Integrations</span>{" "}
              — add your Drive folder, then set up Google access for it
            </li>
            <li>
              <span className="font-medium text-foreground">General</span> — set
              how many Stories per day and your posting window
            </li>
          </ol>
          <Callout type="info" className="mt-3">
            Connecting an Instagram account is not available from the dashboard
            yet. If you need one connected, get in touch — see Step 5.
          </Callout>
        </StepCard>

        <StepCard number={4} title="What happens next">
          <p>Once a folder is connected, Storydump will:</p>
          <ul className="mt-2 space-y-2">
            <li className="flex items-start gap-3">
              <CheckCircle2 className="mt-0.5 h-5 w-5 shrink-0 text-primary" />
              <span>Sync the media in that Drive folder</span>
            </li>
            <li className="flex items-start gap-3">
              <CheckCircle2 className="mt-0.5 h-5 w-5 shrink-0 text-primary" />
              <span>Schedule Stories inside your posting window</span>
            </li>
            <li className="flex items-start gap-3">
              <CheckCircle2 className="mt-0.5 h-5 w-5 shrink-0 text-primary" />
              <span>
                Queue each one for approval — in the dashboard, and in Telegram
                too if you connected it
              </span>
            </li>
          </ul>
          <p className="mt-3">
            Approve, skip, or reject — it&apos;s all up to you.
          </p>
        </StepCard>

        <StepCard number={5} title="Getting help">
          <p>
            Hit a snag? Reach out anytime:
          </p>
          <ul className="mt-2 list-inside list-disc space-y-1">
            <li>
              Email:{" "}
              <a
                href={`mailto:${siteConfig.contact.email}`}
                className="underline underline-offset-4 hover:text-foreground"
              >
                {siteConfig.contact.email}
              </a>
            </li>
            <li>
              Web:{" "}
              <a
                href={siteConfig.contact.portfolio}
                target="_blank"
                rel="noopener noreferrer"
                className="underline underline-offset-4 hover:text-foreground"
              >
                crog.gg
              </a>
            </li>
          </ul>
        </StepCard>
      </div>

      <div className="mt-12 flex items-center justify-between">
        <Link
          href="/setup/media-organize"
          className="inline-flex items-center gap-2 text-sm text-muted-foreground transition-colors hover:text-foreground"
        >
          <ArrowLeft className="h-4 w-4" />
          Organize Media
        </Link>
        <Link
          href="/setup"
          className="inline-flex items-center gap-2 text-sm text-muted-foreground transition-colors hover:text-foreground"
        >
          Back to Overview
        </Link>
      </div>
    </div>
  )
}
