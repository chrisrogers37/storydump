import Link from "next/link"
import { HardDrive, Instagram, MousePointerClick, ScrollText } from "lucide-react"

const promises = [
  {
    icon: HardDrive,
    title: "Read-only Google Drive",
    text: "Storydump can’t change or delete your files. Your originals stay in your Drive.",
  },
  {
    icon: Instagram,
    title: "Instagram’s official login",
    text: "Connect with Instagram. We never see your password.",
  },
  {
    icon: MousePointerClick,
    title: "Nothing posts without a tap",
    text: "Every Story waits for someone on your team.",
  },
  {
    icon: ScrollText,
    title: "Every tap on the record",
    text: "Who did what, and when.",
  },
]

export function TrustRow() {
  return (
    <section aria-label="How Storydump treats your accounts" className="bg-paper py-14 md:py-20">
      <div className="mx-auto max-w-6xl px-4">
        <ul className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
          {promises.map(({ icon: Icon, title, text }) => (
            <li key={title} className="rounded-2xl bg-white p-6">
              <Icon className="size-5 text-ink" />
              <h3 className="mt-4 font-semibold text-ink">{title}</h3>
              <p className="mt-1 text-sm leading-relaxed text-ink/80">{text}</p>
            </li>
          ))}
        </ul>
        <p className="mt-5 text-sm text-ink/70">
          How your media is handled, step by step:{" "}
          <Link href="/privacy" className="font-medium text-ink underline underline-offset-4 hover:text-tap-ink">
            Privacy
          </Link>
          .
        </p>
      </div>
    </section>
  )
}
