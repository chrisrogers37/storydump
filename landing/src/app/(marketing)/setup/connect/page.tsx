import type { Metadata } from "next"
import { noindexMetadata } from "@/lib/seo"
import Link from "next/link"
import { ArrowLeft, CheckCircle2 } from "lucide-react"
import { StepCard } from "@/components/setup/step-card"
import { Callout } from "@/components/setup/callout"
import { UiTerm } from "@/components/setup/ui-term"
import { siteConfig } from "@/config/site"

// Optional and reached from the dashboard, so kept out of search results; it
// still names itself as canonical rather than inheriting another page's.
export const metadata: Metadata = {
  title: "Connect to Telegram",
  description:
    "Optional: link the Storydump Telegram bot to approve Instagram Stories from your phone. Everything also works on the web.",
  alternates: { canonical: "/setup/connect" },
  ...noindexMetadata,
}

export default function ConnectTelegram() {
  return (
    <div>
      <h1 className="font-display text-4xl font-extrabold tracking-[-0.03em] text-ink">
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

        <StepCard number={2} title="Link your Telegram account">
          <p>
            Sign in on the web and open{" "}
            <UiTerm>Settings &rarr; Integrations</UiTerm>. On the Telegram card:
          </p>
          <ol className="mt-2 list-inside list-decimal space-y-1">
            <li>
              Tap <UiTerm>Link Telegram</UiTerm>, then{" "}
              <UiTerm>Open Telegram to finish linking</UiTerm>
            </li>
            <li>
              Tap <UiTerm>Start</UiTerm> in the chat that opens. The bot confirms
              you&apos;re linked.
            </li>
            <li>
              Reload the page. The card shows <UiTerm>Linked</UiTerm>.
            </li>
          </ol>
          <Callout type="info" className="mt-3">
            The link works once and expires after 15 minutes, and whoever taps
            it links their Telegram to your account, so don&apos;t share it.
            Everyone on your team who&apos;ll tap cards links their own account
            the same way.
          </Callout>
        </StepCard>

        <StepCard number={3} title="Add your team's Telegram group">
          <p>
            A workspace admin does this once, after linking. On the same card,
            under <UiTerm>Telegram groups</UiTerm>:
          </p>
          <ol className="mt-2 list-inside list-decimal space-y-1">
            <li>
              Tap <UiTerm>Add a Telegram group</UiTerm>, then{" "}
              <UiTerm>Open Telegram to choose a group</UiTerm>
            </li>
            <li>Pick your team&apos;s group. Telegram adds the bot to it.</li>
            <li>
              The bot says in the group that it now receives your approval
              cards. Reload the page and the group shows{" "}
              <UiTerm>Bound</UiTerm>.
            </li>
          </ol>
          <Callout type="tip" className="mt-3">
            Bot already in the group? Send the start command the card shows in
            the group instead. A group belongs to one workspace, and a
            workspace can have more than one group.
          </Callout>
        </StepCard>

        <StepCard number={4} title="What happens next">
          <p>
            Once your folders are connected and your schedule is set, Storydump
            will:
          </p>
          <ul className="mt-2 space-y-2">
            <li className="flex items-start gap-3">
              <CheckCircle2 className="mt-0.5 h-5 w-5 shrink-0 text-primary" />
              <span>Sync the media in your Drive folders</span>
            </li>
            <li className="flex items-start gap-3">
              <CheckCircle2 className="mt-0.5 h-5 w-5 shrink-0 text-primary" />
              <span>Schedule Stories inside your posting window</span>
            </li>
            <li className="flex items-start gap-3">
              <CheckCircle2 className="mt-0.5 h-5 w-5 shrink-0 text-primary" />
              <span>
                Bring each one to your Queue on the web, and to your Telegram
                group as a card
              </span>
            </li>
          </ul>
          <p className="mt-3">
            Post it and tap <UiTerm>Posted myself</UiTerm>, or{" "}
            <UiTerm>Skip</UiTerm> or <UiTerm>Reject</UiTerm> it. When direct
            posting is switched on for your workspace,{" "}
            <UiTerm>Post now</UiTerm> puts it on your Story for you. The first
            tap settles it, and the card says who tapped.
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
