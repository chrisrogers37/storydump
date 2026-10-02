"use client"

import { useEffect, useRef, useState } from "react"
import { ApprovalCard, type CardAction } from "@/components/landing/approval-card"
import { StoryArt, artLabels, type ArtKind } from "@/components/landing/story-art"
import { cn } from "@/lib/utils"

/**
 * The hero's demo: today's card in a team's Telegram group, and the visitor
 * taps it. Post now walks through what really happens (posting, the Story,
 * the settled card); the other buttons settle the card the way the product
 * does. It runs entirely in the browser and sends nothing.
 */

const SLOT = "2026-10-02 09:00 Europe/London"
const PHOTOS: ArtKind[] = ["bottle", "plant", "stock", "sunset", "quote", "mug"]

type Stage = "waiting" | "posting" | "story" | "settled"

const OUTCOMES: Record<Exclude<CardAction, "post" | "open">, [string, string]> = {
  posted: [
    `✅ Posted by you · ${SLOT}`,
    "Posted it by hand? One tap keeps the record straight.",
  ],
  skip: [
    `⏭️ Skipped by you · ${SLOT}`,
    "Skipped. It goes back in the line-up for later.",
  ],
  reject: [
    `🚫 Rejected by you · ${SLOT}`,
    "Rejected. It won’t come up again.",
  ],
}

const WAITING = "This card is the whole job. Tap Post now."
const POSTING = "Posting to your Story through Instagram’s official API…"
const POSTED: [string, string] = [
  `✅ Posted by you · ${SLOT}`,
  "It’s on your Story. The next one turns up at 13:30.",
]
const OPEN =
  "In the real card, Open Instagram jumps to the app, for posting it by hand."

export function TapDemo() {
  const [photo, setPhoto] = useState(0)
  const [stage, setStage] = useState<Stage>("waiting")
  const [outcome, setOutcome] = useState<string>()
  const [narration, setNarration] = useState(WAITING)
  const timers = useRef<ReturnType<typeof setTimeout>[]>([])
  const anotherRef = useRef<HTMLButtonElement>(null)
  // The tapped button disappears when the card settles, so keyboard focus
  // moves to "Show me another" rather than falling back to the page.
  const moveFocus = useRef(false)
  const art = PHOTOS[photo]

  useEffect(() => {
    if (stage === "settled" && moveFocus.current) {
      moveFocus.current = false
      anotherRef.current?.focus()
    }
  }, [stage])

  useEffect(() => {
    const pending = timers.current
    return () => pending.forEach(clearTimeout)
  }, [])

  function later(ms: number, fn: () => void) {
    timers.current.push(setTimeout(fn, ms))
  }

  function act(action: CardAction) {
    if (action === "open") {
      setNarration(OPEN)
      return
    }
    moveFocus.current = true
    if (action === "post") {
      setStage("posting")
      setNarration(POSTING)
      later(900, () => setStage("story"))
      later(3900, () => {
        setStage("settled")
        setOutcome(POSTED[0])
        setNarration(POSTED[1])
      })
      return
    }
    const [line, said] = OUTCOMES[action]
    setStage("settled")
    setOutcome(line)
    setNarration(said)
  }

  function another() {
    timers.current.forEach(clearTimeout)
    timers.current = []
    setPhoto((p) => (p + 1) % PHOTOS.length)
    setStage("waiting")
    setOutcome(undefined)
    setNarration(WAITING)
  }

  return (
    <div className="relative mx-auto w-full max-w-[460px] rounded-[2rem] bg-tap px-6 pb-6 pt-12 sm:px-10">
      <p
        aria-hidden="true"
        className="absolute -left-2 top-4 -rotate-3 rounded-lg bg-white px-3 py-1.5 font-mono text-[11px] font-semibold uppercase tracking-[0.12em] text-ink shadow-md sm:-left-4"
      >
        Try it · tap Post now
      </p>

      <div
        role="group"
        aria-label="Demo: a Storydump card in a team's Telegram group"
        className="mx-auto w-full max-w-[300px] overflow-hidden rounded-[2.2rem] border-[7px] border-[#151515] bg-[#dfe6ee] shadow-2xl"
      >
        {stage === "story" ? (
          <div className="relative aspect-[9/16] w-full bg-[#2a2622]">
            {/* The Story: the photo whole, framed 9:16 over a soft blur. */}
            <StoryArt
              kind={art}
              className="absolute inset-0 scale-110 text-[34px] opacity-70 blur-xl"
            />
            <StoryArt
              kind={art}
              className="absolute inset-x-0 top-1/2 aspect-[4/5] -translate-y-1/2 text-[26px]"
            />
            <div className="absolute inset-x-3 top-3 h-0.5 overflow-hidden rounded-full bg-white/40">
              <div className="h-full w-full origin-left bg-white motion-safe:animate-[story-progress_2.8s_linear_both]" />
            </div>
            <p className="absolute left-3 top-6 flex items-center gap-2 text-[12px] font-semibold text-white drop-shadow">
              <span className="flex size-6 items-center justify-center rounded-full border-2 border-white bg-tap text-[9px]">
                EC
              </span>
              example.brand <span className="font-normal text-white/80">now</span>
            </p>
            <p className="sr-only">{artLabels[art]}, on example.brand’s Story.</p>
          </div>
        ) : (
          <div className="flex aspect-[9/16] w-full flex-col">
            <div className="flex items-center gap-2.5 bg-white px-3.5 py-2.5">
              <span className="flex size-8 shrink-0 items-center justify-center rounded-full bg-tap text-[11px] font-bold text-ink">
                EC
              </span>
              <span className="min-w-0 leading-tight">
                <span className="block truncate text-[14px] font-semibold text-ink">
                  Example Co · team
                </span>
                <span className="block truncate text-[11px] text-[#6b7280]">
                  3 members, Storydump
                </span>
              </span>
            </div>
            <div className="flex flex-1 flex-col justify-end gap-2 bg-[radial-gradient(#c9d3de_1px,transparent_1px)] bg-[length:14px_14px] p-2.5">
              <span className="mx-auto rounded-full bg-black/20 px-2.5 py-0.5 text-[11px] font-medium text-white">
                Today
              </span>
              <ApprovalCard
                key={photo}
                art={art}
                slot={SLOT}
                outcome={outcome}
                busy={stage === "posting" ? "🚀 Posting…" : undefined}
                onAction={stage === "waiting" ? act : undefined}
                highlight={stage === "waiting" ? "post" : undefined}
              />
            </div>
          </div>
        )}
      </div>

      <p
        role="status"
        aria-live="polite"
        className="mx-auto mt-5 min-h-[3em] max-w-[320px] text-center text-[15px] font-medium leading-snug text-ink"
      >
        {narration}
      </p>
      <div className="mt-2 flex justify-center">
        <button
          ref={anotherRef}
          type="button"
          onClick={another}
          className={cn(
            "rounded-full border-2 border-ink px-4 py-1.5 text-sm font-semibold text-ink transition-colors hover:bg-ink hover:text-white focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ink",
            stage !== "settled" && "invisible"
          )}
          tabIndex={stage !== "settled" ? -1 : undefined}
          aria-hidden={stage !== "settled" ? true : undefined}
        >
          Show me another
        </button>
      </div>
    </div>
  )
}
