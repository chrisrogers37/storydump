import type { Metadata } from "next"
import type { ReactNode } from "react"
import { pageMetadata } from "@/lib/seo"
import Link from "next/link"
import { siteConfig } from "@/config/site"

const LAST_UPDATED = "October 5, 2026"

const linkClass = "underline underline-offset-4 hover:text-foreground"
const email = siteConfig.contact.email

export const metadata: Metadata = pageMetadata({
  title: "Privacy Policy",
  description: "How Storydump collects, uses, and protects your data.",
  path: "/privacy",
})

function EmailLink() {
  return (
    <a href={`mailto:${email}`} className={linkClass}>
      {email}
    </a>
  )
}

function ExternalLink({ href, children }: { href: string; children: ReactNode }) {
  return (
    <a href={href} target="_blank" rel="noopener noreferrer" className={linkClass}>
      {children}
    </a>
  )
}

function Label({ children }: { children: ReactNode }) {
  return <span className="font-medium text-foreground">{children}</span>
}

function Code({ children }: { children: ReactNode }) {
  return (
    <code className="whitespace-nowrap rounded bg-muted px-1 py-0.5 text-[0.85em]">
      {children}
    </code>
  )
}

export default function PrivacyPolicy() {
  return (
    <div className="mx-auto max-w-3xl px-4 py-16">
      <h1 className="text-3xl font-bold tracking-tight">Privacy Policy</h1>
      <p className="mt-2 text-sm text-muted-foreground">
        Last updated: {LAST_UPDATED}
      </p>
      <p className="mt-4 text-lg text-muted-foreground">
        This policy explains what data Storydump keeps, why, who else sees it,
        how long we keep it, and the rights you have. It describes what our
        software actually does today.
      </p>

      <div className="mt-8 rounded-lg border bg-muted/40 p-5 text-sm leading-relaxed text-muted-foreground">
        <h2 className="text-base font-semibold text-foreground">
          The short version
        </h2>
        <ul className="mt-3 list-disc space-y-2 pl-6">
          <li>We never sell your data, and none of it is used for advertising.</li>
          <li>
            Our Google Drive access is read-only. We keep references to your
            files, not the files; the only lasting copies are the approval
            cards in the Telegram chats you link.
          </li>
          <li>
            The copy of a Story we make for posting is deleted as soon as the
            Story is done, and a cleanup removes any copy older than 48 hours.
          </li>
          <li>
            We encrypt your Google and Instagram access tokens before storing
            them, and keep sign-in sessions and API tokens only as one-way
            hashes.
          </li>
          <li>Each workspace&apos;s data is walled off from every other.</li>
          <li>
            The IP addresses we use to stop abuse are deleted after 7 days.
          </li>
          <li>No tracking cookies, so no cookie banner.</li>
        </ul>
      </div>

      <div className="mt-10 space-y-10 text-sm leading-relaxed text-muted-foreground">
        <section>
          <h2 className="text-xl font-semibold text-foreground">1. Who we are</h2>
          <p className="mt-3">
            Storydump (&quot;Storydump&quot;, &quot;we&quot;, &quot;us&quot;) is
            an independent project run by Christopher Rogers, who decides how
            the personal data described here is used (the &quot;data
            controller&quot; under GDPR). For any privacy question or request,
            email <EmailLink />, which reaches him directly.
          </p>
        </section>

        <section>
          <h2 className="text-xl font-semibold text-foreground">2. Scope</h2>
          <p className="mt-3">
            This policy covers the website at storydump.app (including the
            waitlist and the signed-in dashboard), our API at
            api.storydump.app, the Storydump Telegram bot, and the background
            worker that schedules and posts Stories. The services we use to run
            Storydump are listed in section 7. What they do with data on their
            side is covered by their own policies:{" "}
            <ExternalLink href="https://telegram.org/privacy">Telegram</ExternalLink>,{" "}
            <ExternalLink href="https://privacycenter.instagram.com/policy">
              Instagram
            </ExternalLink>
            ,{" "}
            <ExternalLink href="https://policies.google.com/privacy">Google</ExternalLink>,{" "}
            <ExternalLink href="https://cloudinary.com/privacy">Cloudinary</ExternalLink>{" "}
            and{" "}
            <ExternalLink href="https://posthog.com/privacy">PostHog</ExternalLink>.
          </p>
        </section>

        <section>
          <h2 className="text-xl font-semibold text-foreground">
            3. Information we collect
          </h2>
          <p className="mt-3">Here is everything we keep, and where it comes from:</p>
          <ul className="mt-3 list-disc space-y-3 pl-6">
            <li>
              <Label>Waitlist</Label> — if you join the waitlist:
              <ul className="mt-2 list-[circle] space-y-1 pl-5">
                <li>
                  the email address you enter, when you first joined, and any
                  campaign tags (<Code>utm_source</Code>,{" "}
                  <Code>utm_medium</Code>, <Code>utm_campaign</Code>, up to 100
                  characters each) in the web address of the page where you
                  signed up. Joining again keeps your original entry.
                </li>
                <li>
                  a Telegram message: each time the form accepts a signup, our
                  bot sends the email address and the time to each member of our
                  team who has linked Telegram.
                </li>
                <li>
                  when we invite you, your address, the date and an optional
                  internal note on the list of addresses allowed to create an
                  account.
                </li>
              </ul>
              <p className="mt-2">
                To leave the waitlist, email <EmailLink /> from the address you
                joined with. We&apos;ll delete your entry, your place on that
                list, and the Telegram messages we received about your signup.
              </p>
            </li>
            <li>
              <Label>Account data</Label>
              <ul className="mt-2 list-[circle] space-y-1 pl-5">
                <li>
                  From Google when you sign in: your Google account identifier,
                  your verified email address, your display name, and when you
                  last signed in.
                </li>
                <li>
                  For each signed-in session: a one-way hash of its token and
                  when it was last used, not your IP address or browser. For
                  each API token you create: its name, who created it and when
                  it was last used, with the token itself stored only as a hash.
                </li>
                <li>
                  If you link Telegram: your Telegram user ID and username, or
                  your first name if you have no username. When you start the
                  bot in a chat, we also store that chat&apos;s Telegram ID and
                  whether it is a direct chat or a group, so we can deliver to
                  it.
                </li>
              </ul>
            </li>
            <li>
              <Label>Workspace data</Label> — the names and settings of your
              workspaces; who belongs to each, in what role, and who added or
              removed them; invitations, including the invitee&apos;s email
              address or Telegram ID; who approved, scheduled or cancelled each
              Story; and a log of those actions with who took them and when.
              <p className="mt-2">
                If you have linked Telegram and you speak in, or are added to, a
                Telegram group linked to a workspace, we add you to that
                workspace as a member. Leaving the group does not remove you; a
                workspace owner or admin can, or you can email us.
              </p>
            </li>
            <li>
              <Label>Google Drive content</Label> — only after you connect
              Google Drive, which is a separate step from signing in.
              <ul className="mt-2 list-[circle] space-y-1 pl-5">
                <li>
                  To let you pick a folder, we list the names of the folders in
                  your My Drive and in Shared with me. When you pick one, we
                  look up which folders contain it (their IDs only, not
                  stored) so two connected folders never overlap.
                </li>
                <li>
                  After you pick one, we list the photos and videos in it and
                  its subfolders, reading each file&apos;s ID, name, type, size,
                  modified time, checksum and parent folder.
                </li>
                <li>
                  We do not keep your files. We store a reference to each (its
                  Drive file ID, name, type, checksum, folder and the subfolder
                  it sits in), plus Google&apos;s access grant for your Drive,
                  encrypted.
                </li>
                <li>
                  We download a file when we send its approval card and when we
                  post it. To post a Story, we upload a copy to our media
                  processor, Cloudinary, which frames it for Instagram. We
                  delete that copy as soon as the Story posts, fails or is
                  cancelled, and a cleanup that runs every 6 hours deletes any
                  copy older than 48 hours.
                </li>
                <li>
                  If you link the Storydump Telegram bot, each approval card it
                  sends carries the Story&apos;s photo or video and its file
                  name, which stay in the chat like any other message.
                </li>
              </ul>
            </li>
            <li>
              <Label>Instagram data</Label> — for the Instagram professional
              account you connect, with the{" "}
              <Code>instagram_business_basic</Code> and{" "}
              <Code>instagram_business_content_publish</Code> permissions: an
              encrypted long-lived access token, the account ID and username,
              and posting history (Instagram media IDs and links, times,
              results and any error).
            </li>
            <li>
              <Label>Operational data</Label> — queues, schedules, posting
              hours and content mix preferences.
            </li>
            <li>
              <Label>IP addresses</Label> — when you submit the waitlist form,
              sign in, or connect Google Drive or Instagram, we store your IP
              address with a count of attempts per minute, so we can limit
              repeated tries (for an IPv6 address on the waitlist form, only its
              first 64 bits). We delete these records after 7 days. Our hosting
              providers, Vercel and Railway, also keep request logs (IP address,
              the page requested, the time and, on Vercel, your browser&apos;s
              user agent) under their own retention settings.
            </li>
            <li>
              <Label>Usage analytics</Label> — PostHog, in cookieless mode,
              counts page views on the site and dashboard, and a few actions:
              starting, completing or failing a waitlist signup, opening an FAQ
              answer, clicking a call-to-action or Sign in, and tapping the
              demo. With each one it receives the page&apos;s address without
              its query string, the site you came from (its address only, not
              your search terms), your browser, operating system and device
              type, how long you stayed on and how far you scrolled the previous
              page, and any campaign tags in the page&apos;s address. Invitation
              links (under{" "}
              <Code>/join/</Code>) send nothing, and PostHog receives no names,
              email addresses or account IDs.
            </li>
          </ul>
        </section>

        <section>
          <h2 className="text-xl font-semibold text-foreground">
            4. Google user data — Limited Use disclosure
          </h2>
          <p className="mt-3">
            Storydump&apos;s use and transfer of information received from
            Google APIs to any other app will adhere to the{" "}
            <ExternalLink href="https://developers.google.com/terms/api-services-user-data-policy">
              Google API Services User Data Policy
            </ExternalLink>
            , including the Limited Use requirements.
          </p>
          <p className="mt-3">
            We ask Google for two things. Signing in uses the{" "}
            <Code>openid</Code>, <Code>email</Code> and <Code>profile</Code>{" "}
            permissions, which give us your Google account ID, email address and
            name. Connecting Drive uses Google&apos;s read-only Drive permission,{" "}
            <Code>drive.readonly</Code>. It technically allows reading your
            whole Drive; we only ever open the folders described below, and we
            never change or delete anything in your Drive.
          </p>
          <p className="mt-3 font-medium text-foreground">
            How Storydump uses Google user data:
          </p>
          <ul className="mt-2 list-disc space-y-1 pl-6">
            <li>Your Google account ID, email address and name, only to identify your account.</li>
            <li>
              Listing the folder names in your My Drive and Shared with me so you
              can pick a folder, then the photos and videos in that folder and
              its subfolders so you can build a posting queue.
            </li>
            <li>
              Downloading a file to show it on its approval card and to post it
              to your own Instagram account on your behalf.
            </li>
            <li>
              Storing each file&apos;s ID, name, type, checksum and folder to
              track your queue and avoid posting duplicates.
            </li>
            <li>
              Passing Drive data on only to do these things: a copy of the file
              to Cloudinary and Instagram to post it, and the file and its name
              to the Telegram chats you link.
            </li>
          </ul>
          <p className="mt-3 font-medium text-foreground">
            What we never do with it:
          </p>
          <ul className="mt-2 list-disc space-y-1 pl-6">
            <li>
              Sell Google user data, or transfer it for any purpose other than
              the ones above.
            </li>
            <li>
              Use Google user data for advertising, retargeting, or personalized
              advertising.
            </li>
            <li>
              Allow humans to read Google user data, unless we have your
              explicit consent for a specific file, it is necessary for security
              (e.g., investigating abuse), it is required by law, or the data
              has been aggregated and anonymized for internal operations.
            </li>
            <li>
              Use Google user data to develop, improve, or train generalized
              machine learning models.
            </li>
          </ul>
        </section>

        <section>
          <h2 className="text-xl font-semibold text-foreground">
            5. How we use information
          </h2>
          <ul className="mt-3 list-disc space-y-2 pl-6">
            <li>
              To operate the service: send each Story for approval, then
              schedule and publish it.
            </li>
            <li>
              To run the waitlist: tell our team when someone joins, invite
              people in small batches as spots open, email you when your spot
              is ready, and learn from the campaign tags which links bring
              people to it.
            </li>
            <li>
              To deliver an invitation a teammate sends you, and to let that
              invitation admit you.
            </li>
            <li>To authenticate you and keep your session secure.</li>
            <li>
              To send approval cards (with the Story&apos;s photo or video),
              invitations and status messages to the Telegram chats you link.
            </li>
            <li>
              To keep Storydump safe and working: count attempts per IP address
              to limit abuse, keep a log of who changed what in each workspace,
              and read our services&apos; logs to fix problems.
            </li>
            <li>To understand which pages and links people use (PostHog).</li>
            <li>
              To comply with legal obligations, including responding to lawful
              requests.
            </li>
          </ul>
        </section>

        <section>
          <h2 className="text-xl font-semibold text-foreground">
            6. Legal bases for processing (GDPR Article 6)
          </h2>
          <ul className="mt-3 list-disc space-y-2 pl-6">
            <li>
              <Label>Contract</Label> — delivering the service you signed up
              for, including connecting Google Drive and Instagram, which
              Storydump cannot work without. Disconnecting them stops that
              processing.
            </li>
            <li>
              <Label>Consent</Label> — joining the waitlist. You can withdraw
              at any time by emailing us.
            </li>
            <li>
              <Label>Legitimate interest</Label> — security and abuse prevention
              (rate limits and the workspace action log), understanding which
              pages and links work (PostHog and campaign tags), and telling
              our team about new waitlist signups.
            </li>
            <li>
              <Label>Legal obligation</Label> — responding to valid legal
              process.
            </li>
          </ul>
        </section>

        <section>
          <h2 className="text-xl font-semibold text-foreground">
            7. Sharing &amp; sub-processors
          </h2>
          <p className="mt-3">
            We do not sell or rent your personal data. We share it only with the
            services below, each only for the purpose listed. When you link a
            Telegram group, everyone in it sees the approval cards, including
            each Story&apos;s photo or video and file name.
          </p>
          <div className="mt-4 overflow-x-auto">
            <table className="w-full border-collapse text-left text-sm [&_td]:align-top">
              <thead>
                <tr className="border-b">
                  <th className="py-2 pr-4 font-medium text-foreground">
                    Service
                  </th>
                  <th className="py-2 pr-4 font-medium text-foreground">
                    Purpose
                  </th>
                  <th className="py-2 font-medium text-foreground">Location</th>
                </tr>
              </thead>
              <tbody>
                <tr className="border-b">
                  <td className="py-2 pr-4">Vercel</td>
                  <td className="py-2 pr-4">
                    Hosting the website and dashboard; request logs
                  </td>
                  <td className="py-2">US / global</td>
                </tr>
                <tr className="border-b">
                  <td className="py-2 pr-4">Neon</td>
                  <td className="py-2 pr-4">Our database</td>
                  <td className="py-2">US</td>
                </tr>
                <tr className="border-b">
                  <td className="py-2 pr-4">Railway</td>
                  <td className="py-2 pr-4">
                    Hosting our API and worker; service logs
                  </td>
                  <td className="py-2">US</td>
                </tr>
                <tr className="border-b">
                  <td className="py-2 pr-4">Telegram</td>
                  <td className="py-2 pr-4">
                    Runs the Storydump bot: approval cards to the chats you
                    link, and each waitlist signup&apos;s email address and
                    time to our team
                  </td>
                  <td className="py-2">Global</td>
                </tr>
                <tr className="border-b">
                  <td className="py-2 pr-4">Meta (Instagram)</td>
                  <td className="py-2 pr-4">
                    Connecting your Instagram account and posting your Stories
                  </td>
                  <td className="py-2">Global</td>
                </tr>
                <tr className="border-b">
                  <td className="py-2 pr-4">Google</td>
                  <td className="py-2 pr-4">
                    Sign-in, and reading the Drive folder you choose
                  </td>
                  <td className="py-2">Global</td>
                </tr>
                <tr className="border-b">
                  <td className="py-2 pr-4">Cloudinary</td>
                  <td className="py-2 pr-4">
                    Holding a private copy of a Story&apos;s file and framing it
                    for Instagram
                  </td>
                  <td className="py-2">Global</td>
                </tr>
                <tr>
                  <td className="py-2 pr-4">PostHog</td>
                  <td className="py-2 pr-4">
                    Counting page views and actions on the site and dashboard,
                    without cookies
                  </td>
                  <td className="py-2">US</td>
                </tr>
              </tbody>
            </table>
          </div>
          <p className="mt-3">
            We may also disclose data when required by law, to enforce our terms,
            or to protect the rights, property, or safety of users or the public.
          </p>
        </section>

        <section>
          <h2 className="text-xl font-semibold text-foreground">
            8. Cookies &amp; local storage
          </h2>
          <p className="mt-3">
            Storydump uses four cookies and one browser-storage entry, each
            needed for sign-in, invitations or remembering a choice, plus
            cookieless analytics:
          </p>
          <div className="mt-4 overflow-x-auto max-md:scroll-hint">
            <table className="w-full min-w-[36rem] border-collapse text-left text-sm [&_td]:align-top">
              <thead>
                <tr className="border-b">
                  <th className="py-2 pr-4 font-medium text-foreground">Name</th>
                  <th className="py-2 pr-4 font-medium text-foreground">Type</th>
                  <th className="py-2 pr-4 font-medium text-foreground">
                    Purpose
                  </th>
                  <th className="py-2 font-medium text-foreground">Retention</th>
                </tr>
              </thead>
              <tbody>
                <tr className="border-b">
                  <td className="py-2 pr-4">
                    <Code>sd_session</Code>
                  </td>
                  <td className="py-2 pr-4">HttpOnly cookie</td>
                  <td className="py-2 pr-4">
                    Keeps you signed in; cleared when you sign out
                  </td>
                  <td className="py-2">30 days at most</td>
                </tr>
                <tr className="border-b">
                  <td className="py-2 pr-4">
                    <Code>storydump_workspace</Code>
                  </td>
                  <td className="py-2 pr-4">Cookie</td>
                  <td className="py-2 pr-4">
                    Remembers which workspace you are looking at (holds only its
                    ID)
                  </td>
                  <td className="py-2">30 days</td>
                </tr>
                <tr className="border-b">
                  <td className="py-2 pr-4">
                    <Code>sd_oauth_nonce</Code>
                  </td>
                  <td className="py-2 pr-4">HttpOnly cookie</td>
                  <td className="py-2 pr-4">
                    Protects sign-in from forgery; deleted when sign-in
                    completes
                  </td>
                  <td className="py-2">15 minutes</td>
                </tr>
                <tr className="border-b">
                  <td className="py-2 pr-4">
                    <Code>storydump_invite</Code>
                  </td>
                  <td className="py-2 pr-4">HttpOnly cookie</td>
                  <td className="py-2 pr-4">
                    Carries an invitation through sign-in; deleted when you
                    accept it
                  </td>
                  <td className="py-2">15 minutes</td>
                </tr>
                <tr className="border-b">
                  <td className="py-2 pr-4">
                    <Code>storydump-waitlist-registered</Code>
                  </td>
                  <td className="py-2 pr-4">localStorage</td>
                  <td className="py-2 pr-4">
                    Remembers that this browser joined the waitlist (stores only
                    &quot;true&quot;, never your email)
                  </td>
                  <td className="py-2">Until cleared</td>
                </tr>
                <tr>
                  <td className="py-2 pr-4">PostHog</td>
                  <td className="py-2 pr-4">None (cookieless)</td>
                  <td className="py-2 pr-4">Aggregate analytics</td>
                  <td className="py-2">—</td>
                </tr>
              </tbody>
            </table>
          </div>
          <p className="mt-3">
            None of these is used for advertising or to follow you across other
            sites, so we do not display a cookie banner. PostHog runs in
            cookieless mode and keeps nothing in your browser. According to
            PostHog, it counts unique visitors with a code that changes every
            day and removes your IP address before storing an event, so it
            records no location.
          </p>
        </section>

        <section>
          <h2 className="text-xl font-semibold text-foreground">
            9. Data retention
          </h2>
          <ul className="mt-3 list-disc space-y-2 pl-6">
            <li>
              <Label>Waitlist</Label> — your entry, and your place on the list
              of addresses allowed to create an account, until you ask us to
              remove them.
            </li>
            <li>
              <Label>IP addresses for rate limiting</Label> — deleted after 7
              days.
            </li>
            <li>
              <Label>Copies for posting</Label> — deleted when the Story posts,
              fails or is cancelled; any left over are deleted once they are 48
              hours old, by a cleanup that runs every 6 hours.
            </li>
            <li>
              <Label>Workspace data, queue, media references and posting history</Label>{" "}
              — kept while the workspace exists. Removing a folder or
              disconnecting Google Drive pauses them. When an owner deletes a
              workspace, it and everything in it is permanently deleted 30 days
              later; the log of actions taken in it is kept.
            </li>
            <li>
              <Label>Access tokens</Label> — when you disconnect Google Drive or
              remove an Instagram account, we stop using its token at once, and
              for Drive we also ask Google to revoke our access. The encrypted
              token stays in our database until the workspace is deleted. To
              cut off access on Instagram&apos;s side too, remove Storydump
              under Apps and websites in your Instagram settings.
            </li>
            <li>
              <Label>Your account</Label> — your sign-in details stay until you
              ask us to delete your account (section 14). You can unlink
              Telegram yourself at any time.
            </li>
            <li>
              <Label>Telegram messages</Label> — approval cards and waitlist
              signup messages stay in the Telegram chats they were sent to
              until someone deletes them.
            </li>
            <li>
              <Label>Server logs</Label> — kept by Vercel and Railway under
              their retention settings.
            </li>
            <li>
              <Label>Analytics</Label> — kept by PostHog under its retention
              settings.
            </li>
            <li>
              <Label>Backups</Label> — our database provider keeps a rolling
              history of the database so we can recover from mistakes; deleted
              data drops out of it as the history rolls forward. We also keep a
              one-time copy of the data from our previous system, taken on
              September 17, 2026, when we moved to the current one; we apply
              deletion requests to it by hand.
            </li>
          </ul>
        </section>

        <section>
          <h2 className="text-xl font-semibold text-foreground">10. Your rights</h2>
          <p className="mt-3 font-medium text-foreground">
            If you are in the EEA, UK, or Switzerland (GDPR):
          </p>
          <ul className="mt-2 list-disc space-y-1 pl-6">
            <li>Access — request a copy of the data we hold about you.</li>
            <li>Rectification — correct inaccurate data.</li>
            <li>Erasure — request deletion (subject to legal exceptions).</li>
            <li>Restriction — limit how we process your data.</li>
            <li>Portability — receive your data in a portable format.</li>
            <li>Objection — object to processing based on legitimate interest.</li>
            <li>Withdraw consent — at any time, without affecting prior processing.</li>
            <li>
              Lodge a complaint — with your local supervisory authority.
            </li>
          </ul>
          <p className="mt-3 font-medium text-foreground">
            If you are a California resident (CCPA / CPRA):
          </p>
          <ul className="mt-2 list-disc space-y-1 pl-6">
            <li>Right to know what personal information we collect.</li>
            <li>Right to delete personal information.</li>
            <li>Right to correct inaccurate personal information.</li>
            <li>
              Right to opt out of &quot;sale&quot; or &quot;sharing&quot; of
              personal information. Storydump does not sell or share personal
              information as those terms are defined under the CCPA.
            </li>
            <li>Right to non-discrimination for exercising your rights.</li>
          </ul>
          <p className="mt-3 font-medium text-foreground">Children (COPPA):</p>
          <p className="mt-2">
            Storydump is not directed to children under 13, and we do not
            knowingly collect personal information from children under 13. If
            you believe we have collected such data, contact us and we will
            delete it.
          </p>
          <p className="mt-3">
            Some controls are already in the dashboard: a workspace owner or
            admin can disconnect Google Drive or remove an Instagram account,
            anyone can unlink their Telegram account, and an owner can delete a
            workspace. For everything else, including a copy of your data, email{" "}
            <EmailLink />. We handle these requests by hand and respond within
            30 days.
          </p>
        </section>

        <section>
          <h2 className="text-xl font-semibold text-foreground">
            11. International transfers
          </h2>
          <p className="mt-3">
            Our database, our servers and our analytics provider, PostHog, are
            in the US, and Telegram, Meta, Google and Cloudinary process data worldwide. Where a
            provider offers the EU&apos;s Standard Contractual Clauses in its
            data processing terms, those terms cover the transfer. Telegram
            offers no such terms, so messages sent through our bot are covered
            by Telegram&apos;s own privacy policy.
          </p>
        </section>

        <section>
          <h2 className="text-xl font-semibold text-foreground">12. Security</h2>
          <ul className="mt-3 list-disc space-y-2 pl-6">
            <li>
              <Label>Encrypted in transit</Label> — the site and API use TLS
              and tell browsers to refuse anything else (HSTS), and our servers
              reach our database and other services over TLS too.
            </li>
            <li>
              <Label>Encrypted at rest</Label> — our database provider encrypts
              all stored data, and we encrypt your Google and Instagram access
              tokens ourselves before storing them.
            </li>
            <li>
              <Label>Hashed secrets</Label> — sign-in sessions and API tokens
              are stored only as one-way hashes, so a copy of our database
              would not let anyone use them.
            </li>
            <li>
              <Label>Walled-off workspaces</Label> — row-level security in the
              database keeps each workspace&apos;s data apart, and our servers
              connect as database users that cannot bypass it.
            </li>
            <li>
              <Label>Least access</Label> — the public waitlist form can add an
              address but cannot read the list, our servers send data only to a
              fixed set of services, Cloudinary copies are private and need a
              signed link, and we ask Google and Instagram only for the
              permissions named in sections 3 and 4.
            </li>
          </ul>
          <p className="mt-3">
            No system is perfectly secure. If you believe you have found a
            security issue, please email <EmailLink />.
          </p>
        </section>

        <section>
          <h2 className="text-xl font-semibold text-foreground">
            13. Revoking Google Drive access
          </h2>
          <p className="mt-3">
            A workspace owner or admin can disconnect Google Drive at any time
            under Settings › Integrations in the dashboard. That stops our use
            of your Drive at once, and we ask Google to revoke our access. To
            confirm, or to revoke it yourself, visit{" "}
            <ExternalLink href="https://myaccount.google.com/permissions">
              myaccount.google.com/permissions
            </ExternalLink>{" "}
            and remove Storydump.
          </p>
        </section>

        <section>
          <h2 className="text-xl font-semibold text-foreground">
            14. Deleting your data
          </h2>
          <p className="mt-3">
            To delete your Storydump account and the data tied to it, email{" "}
            <EmailLink /> from the email address on your account. We do this by
            hand and complete it within 30 days of your request, including
            removing your details from the workspace action log and from the
            copy of our previous system; deleted data then drops out of our
            database backups as they roll forward. In the
            dashboard, a workspace owner can delete a workspace under Settings ›
            General, and it is permanently deleted 30 days later. You can
            unlink Telegram under Settings › Integrations at any time.
          </p>
        </section>

        <section>
          <h2 className="text-xl font-semibold text-foreground">
            15. Changes to this policy
          </h2>
          <p className="mt-3">
            We may update this policy from time to time. The &quot;Last
            updated&quot; date at the top of this page always reflects the most
            recent revision.
          </p>
        </section>

        <section>
          <h2 className="text-xl font-semibold text-foreground">16. Contact</h2>
          <p className="mt-3">
            Questions, requests, or complaints can be sent to <EmailLink />.
          </p>
        </section>
      </div>

      <div className="mt-12 border-t pt-6 text-sm text-muted-foreground">
        <p>
          See also our{" "}
          <Link href="/terms" className={linkClass}>
            Terms of Service
          </Link>
          .
        </p>
      </div>
    </div>
  )
}
