import Link from "next/link"

export function GoogleDriveInstagramIntegration() {
  return (
    <>
      <p>
        Most content teams already have a media library in Google Drive.
        Product photos from the photographer go into a shared folder. The
        designer exports Stories-ready assets there. The social media person
        then downloads them, opens Instagram, and uploads manually.
      </p>
      <p>
        That download-upload loop is the bottleneck. Google Drive and
        Instagram don&apos;t talk to each other natively. No Zapier zap, no
        official integration, no &ldquo;share to Instagram&rdquo; button.
      </p>
      <p>
        Here&apos;s how to close that gap.
      </p>

      <h2>Why Google Drive as a media source?</h2>
      <p>
        Drive has three properties that make it a solid foundation for an
        Instagram pipeline:
      </p>
      <ul>
        <li>
          <strong>15 GB free</strong> — enough for thousands of
          Story-resolution images. No CDN bills.
        </li>
        <li>
          <strong>Shared folders</strong> — your designer drops files in,
          your automation picks them up. No handoffs, no &ldquo;can you
          send me the file?&rdquo; messages.
        </li>
        <li>
          <strong>Mature API</strong> — the Google Drive API supports
          listing, filtering, and downloading files programmatically.
          Combined with OAuth, your automation can read the folder in
          real time.
        </li>
      </ul>

      <h2>The folder-to-category mapping</h2>
      <p>
        The key insight is that folder names map to content categories.
        Structure your Drive like this:
      </p>
      <pre>
        <code>{`Storydump/
├── product-shots/    → "product" category
├── behind-scenes/    → "bts" category
├── memes/            → "memes" category
└── seasonal/         → "seasonal" category`}</code>
      </pre>
      <p>
        Your automation tool scans each folder, indexes the files, and
        assigns a category based on which folder the file lives in. Then a
        configurable mix ratio (say 40% product, 30% BTS, 20% memes, 10%
        seasonal) controls what gets posted when.
      </p>
      <p>
        See the{" "}
        <Link href="/setup/media-organize">media organization guide</Link>
        {" "}for the full folder structure.
      </p>

      <h2>Connecting Drive to the pipeline</h2>
      <p>
        You connect Drive from inside Storydump — authorize access, then pick
        your folder. There is no Google Cloud project to create and no OAuth
        credentials to manage. Storydump requests the{" "}
        <code>drive.readonly</code> scope, the narrowest one that can list and
        download files: it can never modify or delete them. Your media library
        stays safe.
      </p>

      <h2>The sync loop</h2>
      <p>
        Once connected, a background sync loop periodically checks Drive
        for new or changed files:
      </p>
      <ol>
        <li>List files in each configured folder</li>
        <li>Compare against known files (by Drive file ID)</li>
        <li>Index new files with metadata: name, size, MIME type, category</li>
        <li>Mark deleted files as inactive</li>
      </ol>
      <p>
        The sync runs every few minutes. New content appears in the posting
        pipeline automatically — no manual trigger needed.
      </p>

      <h2>From Drive to Instagram</h2>
      <p>
        When it&apos;s time to post, the pipeline downloads the file from
        Drive, uploads it to a temporary CDN (Cloudinary works well), and
        hands the public URL to the Instagram Graph API for publishing.
        After the Story is live, the temporary CDN upload is deleted.
      </p>
      <p>
        The result: your designer drops a file into Google Drive, and within
        minutes it&apos;s available for posting to Instagram — with
        one-tap approval in between.
      </p>
      <p>
        <Link href="/">Storydump</Link> handles this entire flow. Connect
        your Drive, set a schedule, and your Stories post themselves.
      </p>
    </>
  )
}
