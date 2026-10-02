import { cn } from "@/lib/utils"

/**
 * The demo's photos, drawn in CSS so no real account's content appears on
 * the page. Each fills its box; the caller sets the box's size and shape.
 */
export type ArtKind =
  | "bottle"
  | "plant"
  | "stock"
  | "sunset"
  | "quote"
  | "mug"
  | "monday"

export const artLabels: Record<ArtKind, string> = {
  bottle: "A product shot of a green bottle",
  plant: "A potted plant",
  stock: "A graphic reading Back in stock",
  sunset: "A sunset over hills",
  quote: "A quote card reading Made slowly, on purpose",
  mug: "A white mug on a table",
  monday: "A meme reading monday, again.",
}

export function StoryArt({
  kind,
  className,
}: {
  kind: ArtKind
  className?: string
}) {
  return (
    <div
      aria-hidden="true"
      className={cn("relative overflow-hidden", className)}
    >
      {ART[kind]}
    </div>
  )
}

const ART: Record<ArtKind, React.ReactNode> = {
  bottle: (
    <>
      <div className="absolute inset-0 bg-[radial-gradient(circle_at_50%_15%,#fbe3cc,#f2b98f_70%)]" />
      <div className="absolute inset-x-0 bottom-0 h-[32%] bg-gradient-to-b from-[#d98c55] to-[#c77a45]" />
      <div className="absolute bottom-[29%] left-1/2 h-[3%] w-[44%] -translate-x-1/2 rounded-full bg-black/15 blur-[3px]" />
      <div className="absolute bottom-[31%] left-1/2 flex w-[23%] -translate-x-1/2 flex-col items-center">
        <div className="h-[0.9em] w-[45%] bg-[#1d1d1b]" />
        <div className="h-[1em] w-[38%] bg-[#2c5c4a]" />
        <div className="h-[6.5em] w-full rounded-b-md bg-[linear-gradient(90deg,#24503f,#3f7f66_45%,#24503f)]">
          <div className="mt-[1.6em] h-[1.4em] w-full bg-[#f3ece0]" />
        </div>
      </div>
    </>
  ),
  plant: (
    <>
      <div className="absolute inset-0 bg-[#f1e9dc]" />
      <div className="absolute inset-x-0 bottom-0 h-[28%] bg-[#dccbb3]" />
      <div className="absolute bottom-[20%] left-1/2 h-[28%] w-[34%] -translate-x-1/2 rounded-b-[30%] bg-[linear-gradient(90deg,#b45a2c,#d47a45,#b45a2c)]" />
      <div className="absolute bottom-[46%] left-[28%] h-[26%] w-[24%] rounded-full bg-[#2f6b45]" />
      <div className="absolute bottom-[46%] right-[28%] h-[26%] w-[24%] rounded-full bg-[#4c8f5e]" />
      <div className="absolute bottom-[56%] left-1/2 h-[30%] w-[22%] -translate-x-1/2 rounded-full bg-[#3a7a50]" />
    </>
  ),
  stock: (
    <div className="absolute inset-0 flex flex-col justify-between bg-[#2b4cf0] p-[10%]">
      <div className="h-[6%] w-[30%] rounded-full bg-white/90" />
      <p className="font-display text-[1.35em] font-extrabold uppercase leading-[0.9] text-white">
        Back
        <br />
        in
        <br />
        stock
      </p>
    </div>
  ),
  sunset: (
    <>
      <div className="absolute inset-0 bg-gradient-to-b from-[#ff9a6b] to-[#f2566b]" />
      <div className="absolute left-1/2 top-[30%] h-[22%] w-[38%] -translate-x-1/2 rounded-full bg-[#ffe09a]" />
      <div className="absolute -left-[20%] bottom-[-30%] h-[70%] w-[90%] rounded-full bg-[#5b3a9a]" />
      <div className="absolute -right-[25%] bottom-[-35%] h-[80%] w-[95%] rounded-full bg-[#3a2a72]" />
    </>
  ),
  quote: (
    <div className="absolute inset-0 flex items-center justify-center bg-[radial-gradient(circle_at_30%_20%,#2d5a45,#173a2c_70%)] p-[12%]">
      <p className="text-center font-serif text-[0.85em] italic leading-tight text-white/90">
        “Made slowly, on purpose.”
      </p>
    </div>
  ),
  mug: (
    <>
      <div className="absolute inset-0 bg-[#c9d3bd]" />
      <div className="absolute inset-x-0 bottom-0 h-[30%] bg-[#8a6448]" />
      <div className="absolute bottom-[24%] left-1/2 h-[26%] w-[28%] -translate-x-1/2 rounded-b-xl bg-[linear-gradient(90deg,#e9e4da,#fffdf8_45%,#e9e4da)]" />
      <div className="absolute bottom-[32%] left-[62%] h-[12%] w-[10%] rounded-full border-[0.25em] border-[#efebe2]" />
    </>
  ),
  monday: (
    <div className="absolute inset-0 flex items-center justify-center bg-[#ffd43b] p-[10%]">
      <p className="text-center font-display text-[1em] font-extrabold leading-[0.95] text-[#15130f]">
        monday,
        <br />
        again.
      </p>
    </div>
  ),
}
