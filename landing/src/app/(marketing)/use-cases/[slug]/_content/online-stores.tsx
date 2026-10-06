import { TextLink } from "@/components/landing/text-link"
import { ApprovalCard } from "@/components/landing/approval-card"
import { FolderChips, Section, UseCaseLink, type UseCaseContent } from "./shared"

export const onlineStores: UseCaseContent = {
  lede: (
    <>
      Keep product shots, restocks and behind-the-scenes in Google Drive
      folders. Storydump brings your team a Story from them right on time. Tap{" "}
      <strong className="font-semibold text-ink">Post now</strong> and it’s up.
    </>
  ),
  visual: (
    <div className="mx-auto w-full max-w-sm space-y-4">
      <FolderChips names={["Product shots", "Restocks", "Behind the scenes"]} />
      <ApprovalCard art="stock" handle="@exampleshop" slot="2026-10-02 12:00 Europe/London" className="mx-auto max-w-[18rem]" />
    </div>
  ),
  body: (
    <>
      <Section title="Every day, without making it anyone’s whole job">
        <p>
          A small shop has the photos. What it lacks is the time to pick and
          post a Story every day.
        </p>
      </Section>

      <Section title="A folder for each kind of Story">
        <p>
          Keep product shots, restocks and behind-the-scenes in their own{" "}
          <UseCaseLink slug="google-drive-to-instagram-stories">Google Drive folders</UseCaseLink>
          , and choose how often each one shows up. Here’s how to{" "}
          <TextLink href="/setup/media-organize">set up your folders</TextLink>.
        </p>
      </Section>

      <Section title="New products go to the front">
        <p>
          When its folder is up, anything never posted goes first. Then
          whatever has waited longest, so older products come back around.
          Here’s{" "}
          <UseCaseLink slug="evergreen-instagram-stories">how the rotation works</UseCaseLink>
          .
        </p>
      </Section>

      <Section title="Sold out? Skip it or reject it.">
        <p>
          Skip sets a Story aside for later, 45 days by default. Reject means
          it won’t be offered again.
        </p>
      </Section>

      <Section title="Your team taps, in the group chat or on the web">
        <p>
          Each Story arrives as a card in your team’s Telegram group or in the
          Queue on the web, and{" "}
          <UseCaseLink slug="approve-instagram-stories-in-telegram">your team taps</UseCaseLink>
          . The first tap settles it and says who. You can{" "}
          <TextLink href="/">see the daily card</TextLink> on the home page.
        </p>
      </Section>

      <Section title="What Storydump doesn’t do">
        <p>
          It doesn’t connect to your store or your product catalogue: it works
          from the photos and videos in your Google Drive. And it’s just
          Instagram Stories, with no Reels and no feed posts.
        </p>
      </Section>
    </>
  ),
}
