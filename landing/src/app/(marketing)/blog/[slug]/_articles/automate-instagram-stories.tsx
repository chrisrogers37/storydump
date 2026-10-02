import Link from "next/link"

export function AutomateInstagramStories() {
  return (
    <>
      <p>
        If you run an Instagram account for a shop, a page or your own work,
        you know the daily drill: find something to post, crop it to fit,
        remember to post it, and check nobody else already did. For a Story
        that is gone in 24 hours, that is a lot of work.
      </p>
      <p>
        Most of that work can be automated. The one part worth keeping is
        the decision: a person saying &ldquo;yes, this one, today&rdquo;.
        This guide walks through a pipeline that does everything up to that
        tap, from &ldquo;photos sitting in a folder&rdquo; to &ldquo;on your
        Story&rdquo;.
      </p>

      <h2>The three pieces of a Story pipeline</h2>
      <ol>
        <li>
          <strong>A media source</strong>: where your photos and videos live.
          For most small teams that is already a shared Google Drive folder.
        </li>
        <li>
          <strong>An approval step</strong>: somewhere a person sees
          today&apos;s Story and decides. A team chat or a web queue both
          work.
        </li>
        <li>
          <strong>The Instagram API</strong>: Meta&apos;s official way to
          publish a Story from software. It needs an Instagram professional
          account (Business or Creator).
        </li>
      </ol>

      <h2>Step 1: Organize your media source</h2>
      <p>
        Give each kind of content its own folder: product shots,
        behind-the-scenes, memes, seasonal. The folders do two jobs. They
        keep the library tidy, and they let you set a posting mix, such as
        half product shots, a third behind-the-scenes and the rest memes.
      </p>
      <p>
        Then decide how things take turns. A simple rule that works: anything
        never posted goes first, then whatever has gone longest without a
        turn. Give each posted item a rest before it can come back, so your
        best older content returns without repeating every week.
      </p>
      <p>
        See the{" "}
        <Link href="/setup/media-organize">media organization guide</Link>
        {" "}for the folder structure Storydump uses.
      </p>

      <h2>Step 2: Set up an approval step</h2>
      <p>
        Fully unattended posting sounds great until the automation picks a
        half-finished design or a promo that has ended. An approval step is
        the safety net, and it should take one tap.
      </p>
      <p>
        In Storydump each Story arrives as a card, in your team&apos;s
        Telegram group or in the Queue on the web, with four choices:{" "}
        <strong>Post now</strong> publishes it, <strong>Posted myself</strong>{" "}
        records that you posted it by hand, <strong>Skip</strong> puts it
        back for later, and <strong>Reject</strong> means it never comes up
        again. The first tap settles it, and the card says who tapped.
        Nothing posts without a tap.
      </p>

      <h2>Step 3: Connect the Instagram API</h2>
      <p>
        Publishing a Story through the Instagram API takes three calls:
      </p>
      <ol>
        <li>
          <strong>Create a media container</strong>: POST to{" "}
          <code>/{`{account_id}`}/media</code> with{" "}
          <code>media_type=STORIES</code> and a URL Instagram can fetch the
          file from.
        </li>
        <li>
          <strong>Wait until it is ready</strong>: GET the container&apos;s
          status until it reports <code>FINISHED</code>.
        </li>
        <li>
          <strong>Publish</strong>: POST to{" "}
          <code>/{`{account_id}`}/media_publish</code> with the container
          ID.
        </li>
      </ol>
      <p>
        Instagram fetches the file itself, so it has to sit somewhere
        reachable for a moment. Storydump puts a private, temporary copy on
        Cloudinary, framed to the 9:16 Story size without cropping, and
        deletes it once the Story posts or is cancelled; any copy left over
        is deleted once it is 48 hours old. The{" "}
        <Link href="/privacy">Privacy page</Link> describes each step.
      </p>

      <h2>Putting it together</h2>
      <p>
        The whole loop: your library in Google Drive, a schedule you set
        (how many Stories a day, the hours they can go out, your time zone),
        a card for your team when each one is due, and one tap to post it.
      </p>
      <p>
        That is what <Link href="/">Storydump</Link> does. It is in a free
        beta and invites people in small batches;{" "}
        <Link href="/#waitlist">join the waitlist</Link> to get a spot.
      </p>
    </>
  )
}
