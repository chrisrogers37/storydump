import type { Metadata } from "next"
import Link from "next/link"
import { ArrowRight } from "lucide-react"
import { postDateLabel, posts } from "@/lib/blog"
import { pageMetadata } from "@/lib/seo"
import { breadcrumbList } from "@/lib/json-ld"
import { JsonLd } from "@/components/seo/json-ld"

const description =
  "Practical guides on automating Instagram Stories — scheduling, Google Drive workflows, Telegram approvals, and the Instagram Graph API."

export const metadata: Metadata = pageMetadata({
  title: "Blog",
  description,
  path: "/blog",
})

export default function BlogIndex() {
  return (
    <div className="mx-auto max-w-3xl px-4 py-16">
      <JsonLd
        data={breadcrumbList([
          { name: "Home", path: "/" },
          { name: "Blog", path: "/blog" },
        ])}
      />
      <h1 className="page-title text-4xl text-ink md:text-5xl">Blog</h1>
      <p className="mt-4 text-lg text-muted-foreground">{description}</p>

      <div className="mt-12 space-y-10">
        {posts.map((post) => (
          <article key={post.slug} className="group">
            <Link href={`/blog/${post.slug}`} className="block space-y-3">
              <div className="flex items-center gap-3 text-sm text-muted-foreground">
                <time dateTime={post.updated ?? post.date}>{postDateLabel(post)}</time>
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
