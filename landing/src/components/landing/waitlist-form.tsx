"use client"

import { useRef, useState, useSyncExternalStore } from "react"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { cn } from "@/lib/utils"
import { trackEvent } from "@/lib/analytics"
import { type UtmKey, utmFrom } from "@/lib/utm"

interface WaitlistFormProps {
  variant?: "hero" | "footer"
  className?: string
}

const EMAIL_REGEX = /^[^\s@]+@[^\s@]+\.[^\s@]+$/
const STORAGE_KEY = "storydump-waitlist-registered"

// "busy": the list is full for a minute. Shown like an error, but the
// address is fine, so the field is not marked invalid.
type FormStatus = "idle" | "submitting" | "success" | "error" | "busy" | "duplicate"

function getUtmParams(): Partial<Record<UtmKey, string>> {
  if (typeof window === "undefined") return {}
  return utmFrom(new URLSearchParams(window.location.search))
}

/**
 * Whether this browser has signed up before. Read through
 * useSyncExternalStore rather than a useState initialiser: the server cannot
 * see localStorage and renders the form, so a client that rendered the
 * "already on the list" state on its first pass would not match the server
 * HTML (React error #418). The server snapshot is `false`, hydration matches,
 * and the stored value takes over straight after.
 */
function readRegistered(): boolean {
  try {
    return localStorage.getItem(STORAGE_KEY) !== null
  } catch {
    return false
  }
}

// The browser's "storage" event reaches other tabs only, so a signup also
// announces itself here: the page's other form (hero and closing section)
// stops offering the input once either one succeeds.
const REGISTERED_EVENT = "storydump-waitlist-registered"

function subscribeToStorage(onChange: () => void): () => void {
  window.addEventListener("storage", onChange)
  window.addEventListener(REGISTERED_EVENT, onChange)
  return () => {
    window.removeEventListener("storage", onChange)
    window.removeEventListener(REGISTERED_EVENT, onChange)
  }
}

function markRegistered() {
  try {
    localStorage.setItem(STORAGE_KEY, "true")
  } catch {
    // Storage blocked (private mode, a policy): the form still worked.
  }
  window.dispatchEvent(new Event(REGISTERED_EVENT))
}

export function WaitlistForm({
  variant = "hero",
  className,
}: WaitlistFormProps) {
  const [email, setEmail] = useState("")
  const [status, setStatus] = useState<FormStatus>("idle")
  const [message, setMessage] = useState("")
  // "Waitlist Start" counts each form once per page view, on its first focus.
  const started = useRef(false)
  const registered = useSyncExternalStore(
    subscribeToStorage,
    readRegistered,
    () => false
  )
  // A browser that signed up before shows "already on the list" until this
  // visit submits something of its own.
  const seenBefore = status === "idle" && registered
  const shownStatus: FormStatus = seenBefore ? "duplicate" : status
  const shownMessage = seenBefore ? "You're already on the list!" : message
  // Header links and the blog's call to action point at #waitlist: the hero's
  // form. The closing section's form takes its own id so the page has one.
  const anchorId = variant === "hero" ? "waitlist" : "waitlist-footer"

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault()

    if (!email || !EMAIL_REGEX.test(email)) {
      setStatus("error")
      setMessage("Please enter a valid email address.")
      trackEvent("Waitlist Error", { reason: "invalid_email", variant })
      return
    }

    setStatus("submitting")
    const utm = getUtmParams()

    try {
      const res = await fetch("/api/waitlist", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email, ...utm }),
      })

      const data = await res.json()

      if (data.status === "success") {
        setStatus("success")
        setMessage(data.message)
        trackEvent("Waitlist Signup", { variant, ...utm })
        markRegistered()
      } else {
        const busy = data.reason === "busy"
        setStatus(busy ? "busy" : "error")
        setMessage(data.message || "Something went wrong. Please try again.")
        trackEvent("Waitlist Error", {
          reason: busy ? "busy" : "server_error",
          variant,
        })
      }
    } catch {
      setStatus("error")
      setMessage("Something went wrong. Please try again.")
      trackEvent("Waitlist Error", { reason: "network_error", variant })
    }
  }

  if (shownStatus === "success" || shownStatus === "duplicate") {
    return (
      <div
        id={anchorId}
        className={cn(
          "scroll-mt-20",
          variant === "hero" ? "text-left" : "text-center",
          className
        )}
        role="status"
        aria-live="polite"
      >
        <p className="font-display text-2xl font-extrabold tracking-[-0.02em] text-ink">
          {shownMessage.replace(/'/g, "’")}
        </p>
        <p className="mt-2 leading-relaxed text-ink/80">
          We’re inviting people in small batches as spots open, and we’ll
          email you when yours is ready.
        </p>
      </div>
    )
  }

  return (
    <form
      id={anchorId}
      onSubmit={handleSubmit}
      className={cn("w-full scroll-mt-20", className)}
    >
      <div className="flex flex-col gap-2 sm:flex-row sm:items-center sm:rounded-full sm:border sm:border-ink sm:bg-white sm:p-1.5 sm:pl-5 sm:has-[input:focus-visible]:ring-2 sm:has-[input:focus-visible]:ring-ring sm:has-[input:focus-visible]:ring-offset-2">
        <label htmlFor={`waitlist-email-${variant}`} className="sr-only">
          Email address
        </label>
        <Input
          id={`waitlist-email-${variant}`}
          type="email"
          placeholder="you@example.com"
          value={email}
          onFocus={() => {
            if (started.current) return
            started.current = true
            trackEvent("Waitlist Start", { variant })
          }}
          onChange={(e) => {
            setEmail(e.target.value)
            if (status === "error" || status === "busy") setStatus("idle")
          }}
          disabled={status === "submitting"}
          className="h-12 rounded-full sm:flex-1 border-ink bg-white px-5 text-base sm:h-10 sm:border-0 sm:px-0 sm:shadow-none sm:focus-visible:ring-0"
          aria-invalid={status === "error" || undefined}
          aria-describedby={
            status === "error" || status === "busy"
              ? `waitlist-error-${variant}`
              : undefined
          }
          required
        />
        <Button
          type="submit"
          disabled={status === "submitting"}
          size="xl"
          className="text-base max-sm:h-12"
        >
          {status === "submitting" ? "Joining…" : "Join the waitlist"}
        </Button>
      </div>
      {(status === "error" || status === "busy") && (
        <p
          id={`waitlist-error-${variant}`}
          className={cn(
            "mt-2 text-sm font-medium text-balance",
            // The closing form sits on the orange band, where the alarm red
            // reads at 2.5:1; the band's own ink reads at about 6:1.
            variant === "footer" ? "font-semibold text-ink" : "text-alarm"
          )}
          role="alert"
        >
          {message}
        </p>
      )}
    </form>
  )
}
