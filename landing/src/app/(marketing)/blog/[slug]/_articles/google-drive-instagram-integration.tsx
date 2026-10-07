import Link from "next/link"
import { TrackedLink } from "@/components/analytics/tracked-link"
import { blogCta, blogDemo } from "./shared"

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
        That download-and-upload loop is the bottleneck. Here&apos;s how to
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
        Then connect each of those folders on its own. Each folder you
        connect is a group with its own share of the posting mix, say 50%
        product shots, 30% behind-the-scenes and 20% memes; folders inside
        it are just structure. The mix decides which folder is up next, and
        within that folder anything never posted goes first, then whatever
        has gone longest without a turn.
      </p>
      <p>
        See the{" "}
        <Link href="/setup/media-organize">media organization guide</Link>
        {" "}for the full folder structure.
      </p>

      <h2>Connecting Drive</h2>
      <p>
        You connect Drive from inside Storydump: authorize access, then pick
        your folders from My Drive or Shared with me. There is no Google
        Cloud project to create and no credentials to manage. Storydump
        requests the <code>drive.readonly</code> scope, so it can list and
        read your files but never change or delete them. Your originals stay
        in your Drive.
      </p>
      <p>
        Storydump checks your folders about every six hours, and{" "}
        <strong>Sync Now</strong> in the dashboard checks right away.
      </p>

      <h2>From Drive to Instagram</h2>
      <p>
        When a Story is due, Storydump picks it from your folders and brings
        it to your team: in the Queue on the web and, if you use Telegram, as
        a card in your group. When direct posting is switched on for your
        workspace, one tap puts it on your Story through Instagram&apos;s
        official API: <strong>Post now</strong> on the card,{" "}
        <strong>Approve</strong> in the Queue. Otherwise your team posts it
        and taps <strong>Posted myself</strong>.
      </p>
      <p>
        For the API step, a private, temporary copy, framed to the 9:16 Story
        size, sits on Cloudinary until the Story posts, fails or is
        cancelled, and is then deleted; any copy left over is deleted once it
        is 48 hours old. A Telegram card carries a copy of the photo too, and
        it stays in the chat. For how your data is handled, see the{" "}
        <Link href="/privacy">Privacy page</Link>.
      </p>
      <p>
        The result: your designer drops a file into Google Drive, and it
        joins the rotation at the next sync, with one tap from your team
        before anything posts.
      </p>
      <p>
        That is <Link href="/">Storydump</Link>: see{" "}
        <Link href="/use-cases/google-drive-to-instagram-stories">
          Google Drive to Instagram Stories
        </Link>
        . It is in a free beta and invites people in small batches;{" "}
        <TrackedLink href="/#waitlist" track={blogCta}>
          join the waitlist
        </TrackedLink>{" "}
        to get a spot, or{" "}
        <TrackedLink href="/demo" track={blogDemo}>
          see a sample workspace
        </TrackedLink>{" "}
        first. Nothing in it is real, and nothing posts.
      </p>
    </>
  )
}
