import { TextLink } from "@/components/landing/text-link"
import { StoryArt, type ArtKind } from "@/components/landing/story-art"
import { pathForUseCase, type UseCaseSlug } from "@/lib/use-cases"

const audiences: { art: ArtKind; name: string; text: string; useCase?: UseCaseSlug }[] = [
  {
    art: "stock",
    name: "Online shops",
    useCase: "instagram-stories-for-online-stores",
    text: "Product shots, restocks and behind-the-scenes every day, without making it anyone’s whole job.",
  },
  {
    art: "monday",
    name: "Niche and community pages",
    useCase: "evergreen-instagram-stories",
    text: "Years of memes and posts, back in rotation.",
  },
  {
    art: "sunset",
    name: "Creators",
    useCase: "evergreen-instagram-stories",
    text: "A back catalogue that deserves another look.",
  },
  {
    art: "mug",
    name: "Freelancers",
    text: "A few accounts, each with its own schedule and rotation.",
  },
]

export function WhoItsFor() {
  return (
    <section aria-labelledby="who-heading" className="py-16 md:py-24">
      <div className="mx-auto max-w-6xl px-4">
        <p className="kicker text-tap-ink">
          Who it’s for
        </p>
        <h2
          id="who-heading"
          className="mt-3 max-w-3xl section-title"
        >
          Made for small teams with a deep library.
        </h2>
        <ul className="mt-12 grid grid-cols-2 gap-x-4 gap-y-8 lg:grid-cols-4 lg:gap-6">
          {audiences.map(({ art, name, text, useCase }) => (
            <li key={name}>
              <StoryArt kind={art} className="aspect-[4/5] w-full rounded-2xl text-[18px] sm:text-[26px]" />
              <h3 className="mt-4 font-display text-xl font-extrabold tracking-[-0.02em] text-ink">
                {useCase ? (
                  <TextLink href={pathForUseCase(useCase)} className="font-extrabold decoration-2">
                    {name}
                  </TextLink>
                ) : (
                  name
                )}
              </h3>
              <p className="mt-1 text-sm leading-relaxed text-ink/80">{text}</p>
            </li>
          ))}
        </ul>
        <p className="mt-10 inline-block rounded-full border border-ink/15 px-4 py-2 text-sm font-medium text-ink">
          Just Instagram Stories. No Reels, no feed posts, no TikTok.
        </p>
      </div>
    </section>
  )
}
