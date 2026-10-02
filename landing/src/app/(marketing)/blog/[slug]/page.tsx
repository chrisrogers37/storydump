import type { Metadata } from "next"
import { notFound } from "next/navigation"
import Link from "next/link"
import { ArrowLeft } from "lucide-react"
import { posts, getPost } from "@/lib/blog"
import { noindexMetadata, pageMetadata } from "@/lib/seo"
import { blogPosting, breadcrumbList } from "@/lib/json-ld"
import { TrackedLink } from "@/components/analytics/tracked-link"
import { JsonLd } from "@/components/seo/json-ld"
import { AutomateInstagramStories } from "./_articles/automate-instagram-stories"
import { GoogleDriveInstagramIntegration } from "./_articles/google-drive-instagram-integration"
import { TelegramInstagramApproval } from "./_articles/telegram-instagram-approval-workflow"
import { blogCta } from "./_articles/shared"

const articleComponents: Record<string, React.ComponentType> = {
  "automate-instagram-stories": AutomateInstagramStories,
  "google-drive-instagram-integration": GoogleDriveInstagramIntegration,
  "telegram-instagram-approval-workflow": TelegramInstagramApproval,
}

// An unknown slug is a 404 at routing, served by app/not-found.tsx like any
// other unmatched URL, rather than rendered and then refused.
export const dynamicParams = false

export function generateStaticParams() {
  return posts.map((post) => ({ slug: post.slug }))
}

type Params = Promise<{ slug: string }>

export async function generateMetadata({
  params,
}: {
  params: Params
}): Promise<Metadata> {
  const { slug } = await params
  const post = getPost(slug)
  if (!post) return noindexMetadata

  return pageMetadata({
    title: post.seoTitle ?? post.title,
    description: post.description,
    path: `/blog/${slug}`,
    type: "article",
    publishedTime: post.date,
    modifiedTime: post.updated ?? post.date,
  })
}

export default async function BlogPost({ params }: { params: Params }) {
  const { slug } = await params
  const post = getPost(slug)
  if (!post) notFound()

  const Article = articleComponents[slug]
  if (!Article) notFound()

  return (
    <article className="mx-auto max-w-3xl px-4 py-16">
      <JsonLd data={blogPosting(post)} />
      <JsonLd
        data={breadcrumbList([
          { name: "Home", path: "/" },
          { name: "Blog", path: "/blog" },
          { name: post.title, path: `/blog/${post.slug}` },
        ])}
      />
      <Link
        href="/blog"
        className="mb-8 inline-flex items-center gap-1 text-sm text-muted-foreground hover:text-foreground"
      >
        <ArrowLeft className="h-4 w-4" /> Back to blog
      </Link>

      <header className="mb-10">
        <div className="flex items-center gap-3 text-sm text-muted-foreground">
          <time dateTime={post.date}>
            {new Date(post.date).toLocaleDateString("en-US", {
              year: "numeric",
              month: "long",
              day: "numeric",
            })}
          </time>
          <span aria-hidden="true">&middot;</span>
          <span>{post.readTime}</span>
        </div>
        <h1 className="mt-4 font-display text-4xl font-extrabold tracking-[-0.03em] text-ink md:text-5xl">
          {post.title}
        </h1>
        <p className="mt-4 text-lg text-muted-foreground leading-relaxed">
          {post.description}
        </p>
      </header>

      <div className="prose prose-zinc dark:prose-invert max-w-none">
        <Article />
      </div>

      <footer className="mt-16 rounded-lg border bg-muted/50 p-8 text-center">
        <h2 className="text-xl font-semibold">Ready to automate your Stories?</h2>
        <p className="mt-2 text-muted-foreground">
          Storydump picks today&apos;s Story from your own photos and videos and
          brings it to your team on time, in Telegram or on the web. One tap
          posts it. Free during beta.
        </p>
        <TrackedLink
          href="/#waitlist"
          track={blogCta}
          className="mt-4 inline-block rounded-md bg-primary px-6 py-3 text-sm font-medium text-primary-foreground hover:bg-primary/90"
        >
          Join the waitlist
        </TrackedLink>
      </footer>
    </article>
  )
}
