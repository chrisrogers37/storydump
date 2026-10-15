"use client"

import { useState } from "react"
import { Copy, Check } from "lucide-react"
import { cn } from "@/lib/utils"

interface CopyButtonProps {
  value: string
  className?: string
}

type CopyState = "idle" | "copied" | "failed"

/**
 * Why the copy did not happen, said so the person knows what to do instead.
 * `navigator.clipboard` is absent outside a secure context (plain http, some
 * embedded views); inside one the write can still be refused by a permission.
 */
function copyFailedCopy(): string {
  if (typeof window !== "undefined" && window.isSecureContext === false) {
    return "Copying needs a secure (https) page — select the text and copy it by hand."
  }
  return "The browser refused to copy — select the text and copy it by hand."
}

export function CopyButton({ value, className }: CopyButtonProps) {
  const [state, setState] = useState<CopyState>("idle")

  async function handleCopy() {
    try {
      if (!navigator.clipboard) throw new Error("clipboard unavailable")
      await navigator.clipboard.writeText(value)
      setState("copied")
      setTimeout(() => setState("idle"), 2000)
    } catch {
      // Never an unhandled rejection: a failed copy is a state the person can
      // act on (the value is right there to select), not a console error.
      setState("failed")
    }
  }

  return (
    <span
      className={cn(
        "inline-flex max-w-full flex-wrap items-center gap-2 rounded-md bg-muted px-3 py-1.5 font-mono text-sm",
        className
      )}
    >
      <span className="min-w-0 flex-1 truncate">{value}</span>
      <button
        type="button"
        onClick={handleCopy}
        className="shrink-0 text-muted-foreground transition-colors hover:text-foreground"
        aria-label={
          state === "copied"
            ? "Copied"
            : state === "failed"
              ? "Copy failed"
              : "Copy to clipboard"
        }
      >
        {state === "copied" ? (
          <Check className="h-4 w-4 text-green-600" />
        ) : (
          <Copy className="h-4 w-4" />
        )}
      </button>
      {state === "failed" && (
        <span role="status" className="basis-full font-sans text-xs text-amber-800">
          {copyFailedCopy()}
        </span>
      )}
    </span>
  )
}
