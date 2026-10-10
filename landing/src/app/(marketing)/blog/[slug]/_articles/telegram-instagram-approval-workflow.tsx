import Link from "next/link"
import { TrackedLink } from "@/components/analytics/tracked-link"
import { blogCta } from "./shared"

export function TelegramInstagramApproval() {
  return (
    <>
      <p>
        Every content schedule needs an approval step. Without one,
        you&apos;re one ended promo or half-finished design away from
        posting something you&apos;d rather not. The question is where that
        step should live.
      </p>

      <h2>Where approvals usually end up</h2>
      <ul>
        <li>
          <strong>A busy team chat</strong>: the approval request scrolls
          away under everything else.
        </li>
        <li>
          <strong>Email</strong>: fine for a weekly plan, slow for a Story
          that is due this morning.
        </li>
        <li>
          <strong>A separate app</strong>: one more login for a decision
          that takes two seconds.
        </li>
      </ul>
      <p>
        A good approval step shows you the actual photo, lets you decide in
        one tap, and tells everyone else it&apos;s handled.
      </p>

      <h2>Why a Telegram group works well</h2>
      <ul>
        <li>
          <strong>Buttons under the photo</strong>: a Telegram bot can send
          the photo with its choices right below it. One tap and it&apos;s
          decided.
        </li>
        <li>
          <strong>The real photo</strong>: you see what will go out, not a
          file name.
        </li>
        <li>
          <strong>Notifications</strong>: the card arrives on your phone when
          the Story is due.
        </li>
        <li>
          <strong>The whole team</strong>: in a group, anyone can take it,
          and everyone sees who did.
        </li>
      </ul>

      <h2>The approval flow</h2>
      <ol>
        <li>
          On your schedule, Storydump picks the next Story from your Google
          Drive library.
        </li>
        <li>
          The card arrives in your team&apos;s Telegram group: the photo,
          the account it&apos;s for, its time slot, and the buttons{" "}
          <strong>Posted myself</strong>, <strong>Skip</strong>,{" "}
          <strong>Reject</strong> and <strong>Open Instagram</strong>, with{" "}
          <strong>Post now</strong> when direct posting is switched on for
          your workspace.
        </li>
        <li>
          Someone taps. <strong>Post now</strong> publishes it to your Story
          through Instagram&apos;s official API. Without that button,
          someone posts the Story by hand (<strong>Open Instagram</strong> is
          the shortcut) and taps <strong>Posted myself</strong>. <strong>Skip</strong> puts it
          back for later, and <strong>Reject</strong> means it won&apos;t
          come up again.
        </li>
        <li>
          The first tap settles it. The card loses its buttons and says what
          happened, who tapped and when, so nobody posts it twice.
        </li>
      </ol>
      <p>Nothing posts without a tap.</p>

      <h2>Setting it up</h2>
      <p>
        An admin adds your Telegram group to the workspace once. Each
        teammate links their own Telegram account to Storydump, so a tap
        counts as theirs; a tap from someone who hasn&apos;t linked asks
        them to link first.
      </p>

      <h2>Several accounts</h2>
      <p>
        Every card names the Instagram account it&apos;s for. A workspace has
        one schedule and one posting mix, so a brand that needs its own
        schedule gets its own workspace, and its own Telegram group.
      </p>

      <h2>Not on Telegram?</h2>
      <p>
        Telegram is optional. The same Story waits in your Queue on the web,
        with the same decisions, so a team can work from either or both.
        More on the{" "}
        <Link href="/use-cases/approve-instagram-stories-in-telegram">
          Instagram Story approval in Telegram
        </Link>
        .
      </p>
      <p>
        Want the cards on your phone? Get the free app from{" "}
        <a
          href="https://telegram.org/apps"
          target="_blank"
          rel="noopener noreferrer"
        >
          Telegram&apos;s apps page
        </a>
        , create your account in it, then link it to Storydump.
      </p>
      <p>
        <Link href="/">Storydump</Link> is in a free beta and invites people
        in small batches;{" "}
        <TrackedLink href="/#waitlist" track={blogCta}>
          join the waitlist
        </TrackedLink>{" "}
        to get a spot, or{" "}
        <Link href="/demo">see a sample workspace</Link>{" "}
        first. Nothing in it is real, and nothing posts.
      </p>
    </>
  )
}
