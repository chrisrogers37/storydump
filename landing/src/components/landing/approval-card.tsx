import { cn } from "@/lib/utils"
import { StoryArt, type ArtKind } from "@/components/landing/story-art"

/**
 * The approval card as Storydump sends it to a Telegram group: the photo,
 * captioned "📸 @handle" and "Slot: date time zone", with the five buttons.
 * Once someone acts, the buttons go and the caption gains one line saying
 * what happened, who and when. Every string matches the real card
 * (src/services/target/prompts.py); only the handle and names are
 * placeholders.
 */
export type CardAction = "post" | "posted" | "skip" | "reject" | "open"

export const cardButtons: { action: CardAction; label: string }[] = [
  { action: "post", label: "🚀 Post now" },
  { action: "posted", label: "✅ Posted myself" },
  { action: "skip", label: "⏭️ Skip" },
  { action: "reject", label: "🚫 Reject" },
  { action: "open", label: "📱 Open Instagram" },
]

interface ApprovalCardProps {
  art: ArtKind
  slot: string
  /** The settled line, e.g. "✅ Posted by you · …". No buttons once set. */
  outcome?: string
  /** Shown in place of the buttons while a post is in flight. */
  busy?: string
  /** Makes the buttons real buttons; without it they are a picture. */
  onAction?: (action: CardAction) => void
  /** The button a visitor should try first, outlined. */
  highlight?: CardAction
  layout?: "stacked" | "side"
  className?: string
}

export function ApprovalCard({
  art,
  slot,
  outcome,
  busy,
  onAction,
  highlight,
  layout = "stacked",
  className,
}: ApprovalCardProps) {
  const caption = (
    <p className="text-[13px] leading-snug text-[#15130f]">
      📸 @example.brand
      <br />
      Slot: {slot}
      {outcome && (
        <>
          <br />
          <span className="font-medium">{outcome}</span>
        </>
      )}
    </p>
  )

  return (
    <div className={cn("w-full", className)}>
      <div
        className={cn(
          "rounded-2xl bg-white p-2 shadow-sm",
          layout === "side" && "flex items-center gap-3"
        )}
      >
        {layout === "stacked" && (
          <p className="px-1.5 pb-1.5 pt-0.5 text-[13px] font-semibold text-[#c2410c]">
            Storydump
          </p>
        )}
        <StoryArt
          kind={art}
          className={cn(
            "rounded-xl",
            layout === "stacked"
              ? "aspect-[4/5] w-full text-[20px]"
              : "aspect-[9/16] w-20 shrink-0 text-[11px]"
          )}
        />
        <div className={cn(layout === "stacked" && "px-1.5 pb-1 pt-2")}>
          {caption}
        </div>
      </div>
      {busy ? (
        <p
          className="mt-1.5 rounded-xl bg-white/80 py-2.5 text-center text-[13px] font-medium text-[#1f3b57]"
        >
          {busy}
        </p>
      ) : (
        !outcome && (
          <div className="mt-1.5 grid grid-cols-2 gap-1.5">
            {cardButtons.map(({ action, label }) => {
              const style = cn(
                "rounded-xl bg-white/80 py-2 text-center text-[13px] font-medium text-[#1f3b57]",
                action === "open" && "col-span-2",
                highlight === action && "ring-2 ring-[#15130f]/70"
              )
              return onAction ? (
                <button
                  key={action}
                  type="button"
                  onClick={() => onAction(action)}
                  className={cn(
                    style,
                    "cursor-pointer transition-colors hover:bg-white focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[#15130f]"
                  )}
                >
                  {label}
                </button>
              ) : (
                <span key={action} className={style}>
                  {label}
                </span>
              )
            })}
          </div>
        )
      )}
    </div>
  )
}
