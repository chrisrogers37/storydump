const chores = [
  {
    chore: "Hunt for something to post.",
    answer:
      "Storydump picks from your library: anything never posted first, then whatever has gone longest without a turn.",
  },
  {
    chore: "Crop it to fit.",
    answer: "Photos and videos are sized for Stories, 9:16, without cropping.",
  },
  {
    chore: "Remember to post today.",
    answer: "It shows up on your schedule, in your time zone.",
  },
  {
    chore: "Ask the group chat who’s on it.",
    answer:
      "Your whole team sees it. The first tap settles it, and the card says who.",
  },
]

export function Chores() {
  return (
    <section aria-labelledby="chores-heading" className="bg-paper py-16 md:py-24">
      <div className="mx-auto grid max-w-6xl gap-10 px-4 md:grid-cols-[1fr_1.15fr] md:gap-16">
        <h2
          id="chores-heading"
          className="font-display text-4xl font-extrabold leading-[0.95] tracking-[-0.035em] text-ink sm:text-5xl md:text-6xl"
        >
          Daily Stories, <br className="hidden md:inline" />
          minus the <br className="hidden md:inline" />
          daily chore.
        </h2>
        <div>
          <ul className="border-t border-ink">
            {chores.map(({ chore, answer }) => (
              <li key={chore} className="border-b border-ink/10 py-6">
                <p className="font-display text-2xl font-semibold tracking-[-0.02em] text-ink/60 line-through decoration-tap decoration-[3px] sm:text-[1.7rem]">
                  <span className="sr-only">No more: </span>
                  {chore}
                </p>
                <p className="mt-2 max-w-lg leading-relaxed text-ink/80">{answer}</p>
              </li>
            ))}
          </ul>
          <p className="mt-8 font-display text-4xl font-extrabold tracking-[-0.035em] text-ink sm:text-5xl">
            What’s left:{" "}
            <span className="whitespace-nowrap rounded-lg bg-tap px-2.5">
              one tap.
            </span>
          </p>
        </div>
      </div>
    </section>
  )
}
