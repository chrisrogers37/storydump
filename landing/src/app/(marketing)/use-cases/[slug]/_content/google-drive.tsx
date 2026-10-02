import { ApprovalCard } from "@/components/landing/approval-card"
import { FolderChips, Section, TextLink, UseCaseLink, type UseCaseContent } from "./shared"

export const googleDrive: UseCaseContent = {
  lede: (
    <>
      Connect a Google Drive folder, read-only. Storydump picks today’s Story
      from it, gets it ready and brings it to your team right on time. Tap{" "}
      <strong className="font-semibold text-ink">Post now</strong> and it’s up.
    </>
  ),
  visual: (
    <div className="space-y-4">
      <FolderChips names={["Product shots", "Behind the scenes", "Memes"]} />
      <ApprovalCard art="bottle" slot="2026-10-02 09:00 Europe/London" className="max-w-[18rem]" />
    </div>
  ),
  body: (
    <>
      <Section title="Your photos are in Drive. Your Story is on your phone.">
        <p>
          Getting one file from a folder onto a Story by hand means finding it,
          downloading it, fitting it and posting it. Every day. And someone has
          to remember to.
        </p>
      </Section>

      <Section title="From folder to Story in three steps">
        <ol className="list-decimal space-y-3 pl-6">
          <li>
            Connect Google Drive, read-only, and choose the folders to post
            from.
          </li>
          <li>
            At each time slot Storydump picks today’s Story: anything never
            posted first, then whatever has waited longest (here’s{" "}
            <UseCaseLink slug="evergreen-instagram-stories">how Storydump picks what’s next</UseCaseLink>
            ). Post now sizes it for Stories, 9:16, with nothing cropped.
          </li>
          <li>
            Your team gets it as a card in{" "}
            <UseCaseLink slug="approve-instagram-stories-in-telegram">your team’s Telegram group</UseCaseLink>{" "}
            or as a row in the Queue on the web. Tap Post now, or post it
            yourself and tap Posted myself. You can{" "}
            <TextLink href="/">try the card on the home page</TextLink>.
          </li>
        </ol>
      </Section>

      <Section title="Folders are your posting mix">
        <p>
          Each folder you connect gets its own share of your Stories, say
          Product shots, Behind the scenes and Memes, and subfolders come
          along with their folder. Here’s how to{" "}
          <TextLink href="/setup/media-organize">organize your folders</TextLink>.
        </p>
      </Section>

      <Section title="Keep a folder full, not a calendar">
        <p>
          Instead of building a calendar post by post, keep adding to the
          folder. Storydump checks your folders about every six hours, and
          Sync in the dashboard checks right away. New files go first, because
          they’ve never been posted.
        </p>
        <p>
          There’s nothing to wire: no spreadsheet, no automation flow, no bot
          to build. For the other ways to get Drive files onto Instagram, read{" "}
          <TextLink href="/blog/google-drive-instagram-integration">Google Drive to Instagram: the missing integration</TextLink>
          .
        </p>
      </Section>

      <Section title="Read-only, on purpose">
        <p>
          Storydump can’t change or delete your files, and your originals stay
          in your Drive. How your media is handled, step by step:{" "}
          <TextLink href="/privacy">Privacy</TextLink>.
        </p>
      </Section>
    </>
  ),
}
