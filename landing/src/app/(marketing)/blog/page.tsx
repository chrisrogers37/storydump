import type { Metadata } from "next"
import Link from "next/link"
import { ArrowRight } from "lucide-react"
import { posts } from "@/lib/blog"
import { ogMeta } from "@/lib/og"

const description =
  "Practical guides on automating Instagram Stories — scheduling, Google Drive workflows, Telegram approvals, and the Instagram Graph API."

export const metadata: Metadata = {
  title: "Blog",
  description,
  alternates: { canonical: "/blog" },
  ...ogMeta("Storydump Blog", description),
}

export default function BlogIndex() {
  return (
    <div className="mx-auto max-w-3xl px-4 py-16">
      <h1 className="text-4xl font-bold tracking-tight">Blog</h1>
      <p className="mt-4 text-lg text-muted-foreground">{description}</p>

      <div className="mt-12 space-y-10">
        {posts.map((post) => (
          <article key={post.slug} className="group">
            <Link href={`/blog/${post.slug}`} className="block space-y-3">
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
              <h2 className="text-2xl font-semibold tracking-tight group-hover:underline">
                {post.title}
              </h2>
              <p className="text-muted-foreground leading-relaxed">
                {post.description}
              </p>
              <span className="inline-flex items-center gap-1 text-sm font-medium text-primary">
                Read more <ArrowRight className="h-4 w-4" />
              </span>
            </Link>
          </article>
        ))}
      </div>
    </div>
  )
}
