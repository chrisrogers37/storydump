import Link from "next/link"

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
          <strong>Post now</strong>, <strong>Posted myself</strong>,{" "}
          <strong>Skip</strong>, <strong>Reject</strong> and{" "}
          <strong>Open Instagram</strong>.
        </li>
        <li>
          Someone taps. <strong>Post now</strong> publishes it to your Story
          through Instagram&apos;s official API. <strong>Posted myself</strong>{" "}
          records that you posted it by hand. <strong>Skip</strong> puts it
          back for later, and <strong>Reject</strong> means it won&apos;t
          come up again.
        </li>
        <li>
          The first tap settles it. The card loses its buttons and says what
          happened, who tapped and when, so nobody posts it twice.
        </li>
      </ol>
      <p>Nothing posts without a tap.</p>

      <h2>Several accounts</h2>
      <p>
        Each card names the Instagram account it&apos;s for, and each
        account has its own schedule and rotation.
      </p>

      <h2>Not on Telegram?</h2>
      <p>
        Telegram is optional. The same Story waits in your Queue on the web,
        with the same choices, so a team can work from either or both. More
        on the{" "}
        <Link href="/use-cases/approve-instagram-stories-in-telegram">
          Instagram Story approval in Telegram
        </Link>
        .
      </p>
      <p>
        <Link href="/">Storydump</Link> is in a free beta and invites people
        in small batches; <Link href="/#waitlist">join the waitlist</Link> to
        get a spot.
      </p>
    </>
  )
}
