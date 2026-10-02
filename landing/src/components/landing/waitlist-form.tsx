"use client"

import { useState, useSyncExternalStore } from "react"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { cn } from "@/lib/utils"
import { trackEvent, UTM_KEYS } from "@/lib/analytics"

interface WaitlistFormProps {
  variant?: "hero" | "footer"
  className?: string
}

const EMAIL_REGEX = /^[^\s@]+@[^\s@]+\.[^\s@]+$/
const STORAGE_KEY = "storydump-waitlist-registered"

type FormStatus = "idle" | "submitting" | "success" | "error" | "duplicate"

function getUtmParams(): Record<string, string> {
  if (typeof window === "undefined") return {}
  const params = new URLSearchParams(window.location.search)
  const utm: Record<string, string> = {}
  for (const key of UTM_KEYS) {
    const val = params.get(key)
    if (val) utm[key] = val
  }
  return utm
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

function subscribeToStorage(onChange: () => void): () => void {
  window.addEventListener("storage", onChange)
  return () => window.removeEventListener("storage", onChange)
}

function markRegistered() {
  try {
    localStorage.setItem(STORAGE_KEY, "true")
  } catch {
    // Storage blocked (private mode, a policy): the form still worked.
  }
}

export function WaitlistForm({
  variant = "hero",
  className,
}: WaitlistFormProps) {
  const [email, setEmail] = useState("")
  const [status, setStatus] = useState<FormStatus>("idle")
  const [message, setMessage] = useState("")
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
        if (data.alreadyRegistered) {
          setStatus("duplicate")
          setMessage(data.message)
        } else {
          setStatus("success")
          setMessage(data.message)
          trackEvent("Waitlist Signup", { variant, ...utm })
        }
        markRegistered()
      } else {
        setStatus("error")
        setMessage(data.message || "Something went wrong. Please try again.")
        trackEvent("Waitlist Error", { reason: "server_error", variant })
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
        id="waitlist"
        className={cn(
          variant === "hero" ? "text-left" : "text-center",
          className
        )}
        role="status"
        aria-live="polite"
      >
        <p className="font-display text-2xl font-extrabold tracking-[-0.02em] text-ink">
          {shownMessage}
        </p>
        <p className="mt-2 leading-relaxed text-ink/80">
          We&apos;re inviting people in small batches as spots open, and
          we&apos;ll email you when yours is ready.
        </p>
      </div>
    )
  }

  return (
    <form
      id="waitlist"
      onSubmit={handleSubmit}
      className={cn("w-full", className)}
    >
      <div className="flex flex-col gap-2 sm:flex-row sm:items-center sm:rounded-full sm:border sm:border-ink sm:bg-white sm:p-1.5 sm:pl-5">
        <label htmlFor={`waitlist-email-${variant}`} className="sr-only">
          Email address
        </label>
        <Input
          id={`waitlist-email-${variant}`}
          type="email"
          placeholder="you@example.com"
          value={email}
          onChange={(e) => {
            setEmail(e.target.value)
            if (status === "error") setStatus("idle")
          }}
          disabled={status === "submitting"}
          className="h-12 flex-1 rounded-full border-ink bg-white px-5 text-base sm:h-10 sm:border-0 sm:px-0 sm:shadow-none sm:focus-visible:ring-0"
          aria-invalid={status === "error" || undefined}
          aria-describedby={
            status === "error" ? `waitlist-error-${variant}` : undefined
          }
          required
        />
        <Button
          type="submit"
          disabled={status === "submitting"}
          className="h-12 rounded-full bg-ink px-6 text-base font-semibold text-white hover:bg-ink/85 sm:h-11"
        >
          {status === "submitting" ? "Joining…" : "Join the waitlist"}
        </Button>
      </div>
      {status === "error" && (
        <p
          id={`waitlist-error-${variant}`}
          className="mt-2 text-sm font-medium text-[#9f1d1d]"
          role="alert"
        >
          {shownMessage}
        </p>
      )}
    </form>
  )
}
