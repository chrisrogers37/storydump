import { TextLink } from "@/components/landing/text-link"
import { NextUp } from "@/components/landing/set-it-once"
import { Section, UseCaseLink, type UseCaseContent } from "./shared"

const rules = [
  { rule: "New goes first.", text: "Anything never posted leads the line." },
  { rule: "Then the longest wait.", text: "After that, whatever has gone longest without a turn." },
  { rule: "Thirty days’ rest.", text: "Anything posted sits out 30 days before it can come up again." },
  { rule: "Skip means later.", text: "A skipped Story sits out 45 days, then rejoins the line." },
  { rule: "Reject means never.", text: "It won’t come up again." },
]

export const evergreen: UseCaseContent = {
  lede: (
    <>
      Storydump turns the photos and videos you already have into Instagram
      Stories, one or a few a day, each in its turn.{" "}
      <UseCaseLink slug="approve-instagram-stories-in-telegram">Your team taps</UseCaseLink>{" "}
      to post.
    </>
  ),
  visual: (
    <div className="rounded-3xl bg-ink p-5 sm:p-6">
      <p id="next-up-label" className="kicker mb-4 text-tap">Next five in line</p>
      <NextUp labelledBy="next-up-label" />
    </div>
  ),
  body: (
    <>
      <Section title="A Story posts once. The photo is still good.">
        <p>
          A Story doesn’t stay up for long, so a good shot goes out once and
          then sits in a folder.
        </p>
      </Section>

      <Section title="What counts as evergreen">
        <p>
          Anything you’d happily post again: product shots, behind-the-scenes,
          memes, a back catalogue.
        </p>
      </Section>

      <Section title="Fair to every photo">
        <p>
          Storydump picks by the same five rules every day. You can{" "}
          <TextLink href="/">see today’s card on the home page</TextLink>.
        </p>
        <ol className="space-y-2">
          {rules.map(({ rule, text }) => (
            <li key={rule}>
              <strong className="font-semibold text-ink">{rule}</strong> {text}
            </li>
          ))}
        </ol>
        <p>Both day counts are defaults you can change in Settings.</p>
      </Section>

      <Section title="Your mix decides which folder is up">
        <p>
          <UseCaseLink slug="google-drive-to-instagram-stories">Connect a Google Drive folder</UseCaseLink>{" "}
          for each kind of content and give each its share. The mix decides
          which folder is up, and the first two rules pick within it.
        </p>
      </Section>

      <Section title="Keep adding, and it keeps going">
        <p>
          Keep a folder full instead of building a calendar. Storydump checks
          your folders about every six hours, and Sync in the dashboard checks
          right away. New files go to the front. For{" "}
          <TextLink href="/blog/automate-instagram-stories">how the daily routine fits together</TextLink>
          , read the guide.
        </p>
      </Section>

      <Section title="How long your library lasts">
        <p>
          Count the photos and videos you’d happily post again, then divide by
          Stories a day. That’s your runway. Anything posted rests 30 days, so
          a small library needs fewer Stories a day, or a few more files.
        </p>
      </Section>

      <Section title="Who keeps a deep library">
        <p>
          <UseCaseLink slug="instagram-stories-for-online-stores">Online shops</UseCaseLink>
          , niche and community pages, creators, and freelancers with a few
          accounts, each with its own schedule and rotation.
        </p>
      </Section>
    </>
  ),
}
