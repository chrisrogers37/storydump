import Link from "next/link"

export function GoogleDriveInstagramIntegration() {
  return (
    <>
      <p>
        Most small teams already keep their media in Google Drive. The
        photographer&apos;s product shots go into a shared folder, and so do
        the designer&apos;s exports. Then someone downloads them, opens
        Instagram and uploads them by hand.
      </p>
      <p>
        That download-and-upload loop is the bottleneck, because Google
        Drive has no button that posts to Instagram. Here&apos;s how to
        close the gap.
      </p>

      <h2>Why Google Drive as a media source?</h2>
      <ul>
        <li>
          <strong>It is where the files already are</strong>, so nobody has
          to move a library to a new tool.
        </li>
        <li>
          <strong>Shared folders</strong>: your designer drops files in, and
          they are ready for the schedule. No &ldquo;can you send me the
          file?&rdquo; messages.
        </li>
        <li>
          <strong>A read-only connection</strong>: a tool can read a folder
          without being able to change or delete anything in it.
        </li>
      </ul>

      <h2>The folder-to-category mapping</h2>
      <p>
        The useful trick is to let folders stand for kinds of content.
        Structure your Drive like this:
      </p>
      <pre>
        <code>{`Storydump/
├── product-shots/
├── behind-scenes/
├── memes/
└── seasonal/`}</code>
      </pre>
      <p>
        Then give each folder a share of the posting mix, say 50% product
        shots, 30% behind-the-scenes and 20% memes. The mix decides which
        folder is up next, and within that folder anything never posted goes
        first, then whatever has gone longest without a turn.
      </p>
      <p>
        See the{" "}
        <Link href="/setup/media-organize">media organization guide</Link>
        {" "}for the full folder structure.
      </p>

      <h2>Connecting Drive</h2>
      <p>
        You connect Drive from inside Storydump: authorize access, then pick
        your folders. There is no Google Cloud project to create and no
        credentials to manage. Storydump requests the{" "}
        <code>drive.readonly</code> scope, so it can list and read your
        files but never change or delete them. Your originals stay in your
        Drive.
      </p>

      <h2>From Drive to Instagram</h2>
      <p>
        When a Story is due, Storydump picks it from your folders and brings
        it to your team as a card, in your Telegram group or in the Queue on
        the web. Someone taps <strong>Post now</strong>, and it goes out
        through Instagram&apos;s official API. For that step a private,
        temporary copy, framed to the 9:16 Story size, sits on Cloudinary
        until the Story posts, and is then deleted. The{" "}
        <Link href="/privacy">Privacy page</Link> covers this in full.
      </p>
      <p>
        The result: your designer drops a file into Google Drive, and it
        joins the rotation, with one tap from your team before anything
        posts.
      </p>
      <p>
        That is <Link href="/">Storydump</Link>. It is in a free beta and
        invites people in small batches;{" "}
        <Link href="/#waitlist">join the waitlist</Link> to get a spot.
      </p>
    </>
  )
}
