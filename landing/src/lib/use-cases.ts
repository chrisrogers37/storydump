/**
 * The four /use-cases pages: what each is called, where it lives and what it
 * tells search engines and social cards. The body copy is in
 * `app/(marketing)/use-cases/[slug]/_content/`. Each page owns one search
 * intent the home page can't (landing-qa/phase-3/content-brief.md), and says
 * only what the product does today; `home-claims-contract.test.ts` scans both.
 */
export interface UseCase {
  slug: string
  /** The search title; the root layout's template adds " | Storydump". */
  seoTitle: string
  /** The meta description. */
  description: string
  /** The social card's lines; the subtitle is at most 100 characters. */
  ogTitle: string
  ogSubtitle: string
  /** The mono kicker above the H1. */
  eyebrow: string
  /** The H1. */
  title: string
  /** The page's name in the footer and in "More use cases". */
  navLabel: string
  /** The closing section's heading. */
  closing: string
  lastModified: string
}

export const useCases = [
  {
    slug: "google-drive-to-instagram-stories",
    seoTitle: "Google Drive to Instagram Stories",
    description:
      "Connect a Google Drive folder, read-only. Storydump picks today’s Instagram Story from it and brings it to your team on time. One tap posts it.",
    ogTitle: "Google Drive to Instagram Stories",
    ogSubtitle: "Storydump picks today’s Story from your Drive folders and brings it to your team on time.",
    eyebrow: "Use case · Google Drive",
    title: "From Google Drive to Instagram Stories, one tap at a time",
    navLabel: "Google Drive",
    closing: "Your next Story is already in your Drive.",
    lastModified: "2026-10-07",
  },
  {
    slug: "evergreen-instagram-stories",
    seoTitle: "Evergreen Instagram Stories From Your Library",
    description:
      "Your best photos deserve more than one Story. Storydump recycles your library into Instagram Stories: new first, then whatever has waited longest.",
    ogTitle: "Evergreen content, back on your Story",
    ogSubtitle: "Never-posted first, then whatever has waited longest. Nothing gets buried.",
    eyebrow: "Use case · Evergreen content",
    title: "Recycle your evergreen content into Instagram Stories",
    navLabel: "Evergreen content",
    closing: "Put your archive back on your Story.",
    lastModified: "2026-10-07",
  },
  {
    slug: "approve-instagram-stories-in-telegram",
    seoTitle: "Instagram Story Approval Workflow in Telegram",
    description:
      "A one-tap approval workflow for Instagram Stories. Each Story reaches your team’s Telegram group or web Queue as a card, and the first tap settles it.",
    ogTitle: "Approve Stories in your team’s Telegram",
    ogSubtitle: "One card per Story. Anyone on the team can tap it, and the card says who did.",
    eyebrow: "Use case · Team approvals",
    title: "Approve Instagram Stories in your team’s Telegram group",
    navLabel: "Team approvals",
    closing: "One card. Anyone on the team can take it.",
    lastModified: "2026-10-07",
  },
  {
    slug: "instagram-stories-for-online-stores",
    seoTitle: "Instagram Stories for Small Online Stores",
    description:
      "Daily Instagram Stories for your shop, picked from the product shots, restocks and behind-the-scenes in your Google Drive. Your team taps to post.",
    ogTitle: "Instagram Stories for small online stores",
    ogSubtitle: "Product shots, restocks and behind-the-scenes from your own Drive, every day.",
    eyebrow: "Use case · Online stores",
    title: "Daily Instagram Stories for your online store",
    navLabel: "Online stores",
    closing: "Your product shots are tomorrow’s Stories.",
    lastModified: "2026-10-07",
  },
] as const satisfies readonly UseCase[]

export type UseCaseSlug = (typeof useCases)[number]["slug"]

/** The index page that lists them all. */
export const useCasesPath = "/use-cases"

export const pathForUseCase = (slug: UseCaseSlug) => `${useCasesPath}/${slug}`

export function getUseCase(slug: string) {
  return useCases.find((useCase) => useCase.slug === slug)
}
