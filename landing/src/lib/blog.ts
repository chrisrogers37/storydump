export interface BlogPost {
  slug: string
  title: string
  description: string
  /** First published, YYYY-MM-DD. */
  date: string
  /** Set when the content changes after publication, YYYY-MM-DD. */
  updated?: string
  /** A shorter title for search results, when `title` plus " | Storydump" runs past 60 characters. */
  seoTitle?: string
  readTime: string
  keywords: string[]
}

export const posts: BlogPost[] = [
  {
    slug: "automate-instagram-stories",
    title: "How to Automate Instagram Stories in 2026",
    description:
      "Automate everything up to the tap: a Google Drive library, a schedule you set and a one-tap approval in Telegram or on the web, step by step.",
    date: "2026-05-25",
    updated: "2026-10-02",
    readTime: "6 min read",
    keywords: [
      "automate instagram stories",
      "instagram story automation",
      "auto post instagram stories",
      "schedule instagram stories automatically",
    ],
  },
  {
    slug: "google-drive-instagram-integration",
    title: "Google Drive to Instagram: The Missing Integration",
    seoTitle: "Google Drive to Instagram Stories",
    description:
      "Your media lives in Google Drive and your audience on Instagram. Here's how to post Stories from a Drive folder without downloading and re-uploading.",
    date: "2026-05-25",
    updated: "2026-10-02",
    readTime: "5 min read",
    keywords: [
      "google drive instagram integration",
      "instagram google drive",
      "upload google drive to instagram",
      "instagram content from google drive",
    ],
  },
  {
    slug: "telegram-instagram-approval-workflow",
    title: "Approving Instagram Stories in Telegram",
    description:
      "A one-tap approval step for your team's Instagram Stories, in a Telegram group or on the web: see the photo, tap Post now, and everyone sees who did.",
    date: "2026-05-25",
    updated: "2026-10-02",
    readTime: "4 min read",
    keywords: [
      "telegram bot for instagram",
      "instagram content approval",
      "telegram instagram bot",
      "instagram story approval workflow",
    ],
  },
]

export function getPost(slug: string): BlogPost | undefined {
  return posts.find((p) => p.slug === slug)
}
