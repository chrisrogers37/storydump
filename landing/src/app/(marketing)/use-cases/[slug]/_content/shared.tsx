import { TextLink } from "@/components/landing/text-link"
import type { ReactNode } from "react"
import { pathForUseCase, type UseCaseSlug } from "@/lib/use-cases"

/** One page's copy: the lede under the H1, its picture, and the sections. */
export interface UseCaseContent {
  lede: ReactNode
  visual: ReactNode
  body: ReactNode
}

export function UseCaseLink({ slug, children }: { slug: UseCaseSlug; children: ReactNode }) {
  return <TextLink href={pathForUseCase(slug)}>{children}</TextLink>
}

export function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section className="mt-14 first:mt-0">
      <h2 className="page-title text-3xl text-ink md:text-4xl">
        {title}
      </h2>
      <div className="mt-4 space-y-4 text-lg leading-relaxed text-ink/80">{children}</div>
    </section>
  )
}

/** Drive folders as chips, each marked connected. No shares: no new numbers. */
export function FolderChips({ names }: { names: string[] }) {
  return (
    <ul aria-label="Connected Google Drive folders" className="flex flex-wrap justify-center gap-2">
      {names.map((name) => (
        <li
          key={name}
          className="flex items-center gap-2 rounded-xl bg-white px-3 py-2 font-mono text-xs text-ink shadow-sm"
        >
          <span aria-hidden="true">📁</span>
          {name}
          <span className="rounded-full bg-[#e3f5e1] px-2 py-0.5 text-[10px] text-[#1e6b2a]">Connected</span>
        </li>
      ))}
    </ul>
  )
}
