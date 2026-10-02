import { StoryArt, type ArtKind } from "@/components/landing/story-art"

const mix = [
  { name: "Product shots", share: 50, color: "bg-tap" },
  { name: "Behind the scenes", share: 30, color: "bg-[#2b4cf0]" },
  { name: "Memes", share: 20, color: "bg-[#ffd43b]" },
]

const NEW = "Never posted"

const nextUp: { art: ArtKind; note: string }[] = [
  { art: "plant", note: NEW },
  { art: "stock", note: NEW },
  { art: "sunset", note: "Last up 9 weeks ago" },
  { art: "quote", note: "Last up 8 weeks ago" },
  { art: "mug", note: "Last up 7 weeks ago" },
]

function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex items-center justify-between gap-4 border-b border-ink/10 py-4">
      <dt className="text-sm font-medium text-ink">{label}</dt>
      <dd>{children}</dd>
    </div>
  )
}

const chip = "rounded-lg bg-paper px-3 py-1.5 font-mono text-xs text-ink"

export function SetItOnce() {
  return (
    <section aria-labelledby="rhythm-heading" className="bg-paper py-16 md:py-24">
      <div className="mx-auto max-w-6xl px-4">
        <div className="grid items-start gap-10 md:grid-cols-2">
          <div>
            <p className="kicker text-tap-ink">
              Set it once
            </p>
            <h2
              id="rhythm-heading"
              className="mt-3 section-title"
            >
              Your rhythm. <br />
              Your mix.
            </h2>
            <p className="mt-6 max-w-xl text-lg leading-relaxed text-ink/80">
              Choose how many Stories a day, the hours they can go out and your
              time zone. Keep product shots, behind-the-scenes and memes in
              their own folders, and choose how often each one shows up.
            </p>
          </div>

          <figure className="rounded-3xl bg-white p-6 shadow-xl shadow-ink/5">
            <figcaption className="sr-only">Example settings</figcaption>
            <dl>
              <Row label="Stories a day">
                <span className="flex items-center gap-2">
                  <span aria-hidden="true" className="flex size-9 items-center justify-center rounded-lg border border-ink/10 bg-paper text-ink">
                    −
                  </span>
                  <span className="w-6 text-center text-lg font-semibold text-ink">2</span>
                  <span aria-hidden="true" className="flex size-9 items-center justify-center rounded-lg border border-ink/10 bg-paper text-ink">
                    +
                  </span>
                </span>
              </Row>
              <Row label="Posting hours">
                <span className={chip}>09:00 – 18:00</span>
              </Row>
              <Row label="Time zone">
                <span className={chip}>Europe/London</span>
              </Row>
              <div className="pt-4">
                <dt className="text-sm font-medium text-ink">Posting mix</dt>
                <dd>
                  <div aria-hidden="true" className="mt-3 flex gap-1">
                    {mix.map((m) => (
                      <span
                        key={m.name}
                        className={`h-2.5 rounded-full ${m.color}`}
                        style={{ width: `${m.share}%` }}
                      />
                    ))}
                  </div>
                  <ul className="mt-3 space-y-1.5 text-sm">
                    {mix.map((m) => (
                      <li key={m.name} className="flex items-center gap-2 text-ink/80">
                        <span aria-hidden="true" className={`size-2.5 rounded-sm ${m.color}`} />
                        {m.name}
                        <span className="ml-auto font-mono text-xs font-semibold text-ink">
                          {m.share}%
                        </span>
                      </li>
                    ))}
                  </ul>
                </dd>
              </div>
            </dl>
          </figure>
        </div>

        <div className="mt-12 grid items-center gap-8 rounded-3xl bg-ink p-7 md:grid-cols-[1fr_1.4fr] md:p-9">
          <div>
            <h3 className="kicker text-tap">
              Every photo gets its turn
            </h3>
            <p className="mt-3 leading-relaxed text-white/85">
              Anything never posted goes first. Then whatever has gone longest
              without a turn, so your best older stuff comes back around. Keep
              adding, and it keeps going.
            </p>
          </div>
          <ol aria-label="Next five in line" className="grid grid-cols-5 gap-2 sm:gap-3">
            {nextUp.map(({ art, note }, i) => (
              <li key={art}>
                <StoryArt kind={art} className="aspect-[9/16] w-full rounded-lg text-[8px] sm:rounded-xl sm:text-[13px]" />
                <p className={`mt-2 font-mono text-[10px] leading-tight sm:text-[11px] ${note === NEW ? "text-tap" : "text-white/70"}`}>
                  <span className="sr-only">{i + 1}: </span>
                  {note}
                </p>
              </li>
            ))}
          </ol>
        </div>
      </div>
    </section>
  )
}
