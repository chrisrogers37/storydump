import {
  ApprovalCard,
  cardButtons,
  type CardAction,
} from "@/components/landing/approval-card"
import { StoryArt } from "@/components/landing/story-art"
import { ACTION_LABELS, actionsFor } from "@/lib/intents"

const explain: Partial<Record<CardAction, string>> = {
  post: "Publishes it to your Story through Instagram’s official API.",
  posted: "Posted it by hand? One tap keeps the record straight.",
  skip: "Not today. It goes back in the line-up for later.",
  reject: "Not ever. It won’t come up again.",
}

export const cardLegend = cardButtons.flatMap(({ action, label }) =>
  explain[action] ? [{ label, text: explain[action] }] : []
)

// The web Queue's buttons for a story awaiting approval, as the dashboard
// renders them, with Instagram API publishing on.
export const queueButtons = actionsFor("awaiting_approval", true).map(
  (a) => ACTION_LABELS[a]
)

function PanelLabel({ name, tag }: { name: string; tag: string }) {
  return (
    <div className="mt-4 flex items-center justify-between font-mono text-xs">
      <span className="uppercase tracking-[0.14em] text-ink/70">{name}</span>
      <span className="rounded-full bg-paper px-2.5 py-1 text-ink">{tag}</span>
    </div>
  )
}

export function WhereTap() {
  return (
    <section aria-labelledby="where-heading" className="py-16 md:py-24">
      <div className="mx-auto max-w-6xl px-4">
        <p className="kicker text-tap-ink">
          Where the tap happens
        </p>
        <h2
          id="where-heading"
          className="mt-3 max-w-3xl section-title"
        >
          In your team’s Telegram group. Or on the web.
        </h2>
        <p className="mt-6 max-w-2xl text-lg leading-relaxed text-ink/80">
          Link a Telegram group and each Story arrives there as a card anyone on
          your team can act on. Not on Telegram? The same Story waits in your
          Queue on the web, with the same choices.
        </p>

        <div className="mt-12 grid gap-5 md:grid-cols-2">
          <figure className="flex flex-col rounded-3xl border border-ink/10 p-5 sm:p-7">
            <div className="flex min-h-[260px] flex-1 items-center rounded-2xl bg-[#dfe6ee] p-4">
              <ApprovalCard
                art="monday"
                slot="2026-10-02 13:30 Europe/London"
                layout="side"
                className="max-w-sm"
              />
            </div>
            <figcaption>
              <PanelLabel name="In Telegram" tag="Optional" />
            </figcaption>
          </figure>

          <figure className="flex flex-col rounded-3xl border border-ink/10 p-5 sm:p-7">
            <div className="flex-1 rounded-2xl border border-ink/10 bg-[#fbfaf7]">
              <div className="flex items-center justify-between border-b border-ink/10 px-4 py-3 text-sm">
                <span className="font-semibold text-ink">Queue</span>
                <span className="text-ink/60">Today</span>
              </div>
              <div className="flex gap-4 p-4">
                <StoryArt
                  kind="monday"
                  className="aspect-[9/16] w-16 shrink-0 rounded-lg text-[9px]"
                />
                <div className="min-w-0">
                  <p className="truncate text-sm font-semibold text-ink">
                    monday-again.jpg
                  </p>
                  <p className="mt-0.5 text-xs text-ink/70">
                    @example.brand · Fri, Oct 2, 1:30 PM · Memes
                  </p>
                  <span className="mt-2 inline-block rounded-full bg-[#fff1c2] px-2 py-0.5 font-mono text-[11px] text-[#7a5300]">
                    awaiting approval
                  </span>
                  <div aria-hidden="true" className="mt-3 flex flex-wrap gap-1.5 text-xs font-medium">
                    {queueButtons.map((b, i) => (
                      <span
                        key={b}
                        className={
                          i === 0
                            ? "rounded-md bg-ink px-3 py-1.5 text-white"
                            : "rounded-md border border-ink/15 bg-white px-3 py-1.5 text-ink"
                        }
                      >
                        {b}
                      </span>
                    ))}
                  </div>
                  <p className="sr-only">Buttons: {queueButtons.join(", ")}.</p>
                </div>
              </div>
            </div>
            <figcaption>
              <PanelLabel name="On the web" tag="Always on" />
            </figcaption>
          </figure>
        </div>

        <dl className="mt-8 grid grid-cols-2 gap-3 lg:grid-cols-4">
          {cardLegend.map(({ label, text }) => (
            <div key={label} className="rounded-2xl bg-paper p-5">
              <dt className="font-semibold text-ink">{label}</dt>
              <dd className="mt-1 text-sm leading-relaxed text-ink/80">{text}</dd>
            </div>
          ))}
        </dl>
        <p className="mt-5 text-sm text-ink/70">
          Telegram is optional. Everything works on the web.
        </p>
      </div>
    </section>
  )
}
