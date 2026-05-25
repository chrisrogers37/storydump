export interface BlogPost {
  slug: string
  title: string
  description: string
  date: string
  readTime: string
  keywords: string[]
}

export const posts: BlogPost[] = [
  {
    slug: "automate-instagram-stories",
    title: "How to Automate Instagram Stories in 2026",
    description:
      "Manual posting burns hours every week. Here's how to set up a hands-off Instagram Story pipeline with Google Drive, Telegram approvals, and the Instagram Graph API.",
    date: "2026-05-25",
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
    description:
      "Your media library lives in Google Drive. Your audience lives on Instagram. Here's how to bridge the gap without manual downloads, re-uploads, or third-party storage fees.",
    date: "2026-05-25",
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
    title: "Why Telegram Is the Best Instagram Content Approval Tool",
    description:
      "Slack is too noisy. Email is too slow. Telegram bots give you one-tap approve/skip/reject for every Instagram Story — from your phone, in real time.",
    date: "2026-05-25",
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
