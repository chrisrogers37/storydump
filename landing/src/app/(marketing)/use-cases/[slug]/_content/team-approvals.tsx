import { ApprovalCard } from "@/components/landing/approval-card"
import { cardLegend, queueButtons } from "@/components/landing/where-tap"
import { Section, TextLink, UseCaseLink, type UseCaseContent } from "./shared"

const SLOT = "2026-10-02 09:00 Europe/London"

export const teamApprovals: UseCaseContent = {
  lede: (
    <>
      Link a Telegram group and each Story arrives there as a card anyone on
      your team can act on. The first tap settles it, and the card says who.
    </>
  ),
  visual: (
    <div className="grid max-w-xl grid-cols-2 gap-3">
      <figure>
        <ApprovalCard art="monday" slot={SLOT} />
        <figcaption className="kicker mt-3 text-ink/60">Waiting</figcaption>
      </figure>
      <figure>
        <ApprovalCard art="monday" slot={SLOT} outcome="✅ Posted by Sam · 2026-10-02 09:01 Europe/London" />
        <figcaption className="kicker mt-3 text-ink/60">Settled</figcaption>
      </figure>
    </div>
  ),
  body: (
    <>
      <Section title="Who’s posting today?">
        <p>
          When the daily Story lives in a group chat, someone has to ask who’s
          on it. On busy days, nobody is.
        </p>
      </Section>

      <Section title="One card per Story">
        <p>
          Each Story, <UseCaseLink slug="google-drive-to-instagram-stories">picked from your Google Drive</UseCaseLink>{" "}
          (here’s <UseCaseLink slug="evergreen-instagram-stories">how today’s Story is picked</UseCaseLink>
          ), arrives as the real card: the photo, “📸 @example.brand”, its
          slot, and the buttons. You can{" "}
          <TextLink href="/">try the card on the home page</TextLink>.
        </p>
        <dl className="grid gap-x-6 gap-y-3 sm:grid-cols-2">
          {cardLegend.map(({ label, text }) => (
            <div key={label}>
              <dt className="font-semibold text-ink">{label}</dt>
              <dd className="text-base">{text}</dd>
            </div>
          ))}
        </dl>
      </Section>

      <Section title="The first tap settles it">
        <p>
          Anyone in your workspace can act on the card, and people outside it
          can’t. A second tap gets the first tap’s answer, and the card
          settles on “✅ Posted by Sam · 2026-10-02 09:01 Europe/London”, by
          display name, never an email.
        </p>
      </Section>

      <Section title="Nothing posts without a tap">
        <p>
          Every Story waits for someone on your team, and every tap is on the
          record: who did what, and when.
        </p>
      </Section>

      <Section title="Not on Telegram? Use the web.">
        <p>
          The same Story waits in your Queue on the web, marked “awaiting
          approval”, with {queueButtons.slice(0, -1).join(", ")} and {queueButtons.at(-1)}. Telegram is optional.
        </p>
      </Section>

      <Section title="Nothing to wire">
        <p>
          No spreadsheet, no automation flow and no bot to create. After your
          invite, you link your group to Storydump’s bot during setup. For{" "}
          <TextLink href="/blog/telegram-instagram-approval-workflow">why small teams approve in a chat</TextLink>
          , read the guide.
        </p>
      </Section>
    </>
  ),
}
