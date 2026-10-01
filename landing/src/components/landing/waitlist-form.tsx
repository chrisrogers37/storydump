"use client"

import { useState, useSyncExternalStore } from "react"
import Link from "next/link"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { cn } from "@/lib/utils"
import { trackEvent, UTM_KEYS } from "@/lib/analytics"
import { botUrl } from "@/lib/telegram-bot"

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
  const [submitted, setStatus] = useState<FormStatus>("idle")
  const [message, setMessage] = useState("")
  const registered = useSyncExternalStore(
    subscribeToStorage,
    readRegistered,
    () => false
  )
  // A browser that signed up before shows "already on the list" until this
  // visit submits something of its own.
  const status: FormStatus =
    submitted === "idle" && registered ? "duplicate" : submitted
  const shownMessage =
    submitted === "idle" && registered ? "You're already on the list!" : message

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

  if (status === "success" || status === "duplicate") {
    return (
      <div
        id="waitlist"
        className={cn("text-center", className)}
        role="status"
        aria-live="polite"
      >
        <div className="space-y-3">
          <p className="text-lg font-medium">{shownMessage}</p>
          {status === "success" && (
            <>
              <p className="text-sm text-muted-foreground">
                We&apos;re onboarding beta users in small batches. You&apos;ll
                hear from us within a week with setup instructions.
              </p>
              <p className="text-sm text-muted-foreground">
                While you wait, get your side ready: a Google Drive folder with
                your story content, your Instagram switched to a{" "}
                <Link
                  href="/setup/instagram"
                  className="underline underline-offset-4 hover:text-foreground transition-colors"
                >
                  Professional account
                </Link>
                , and Telegram. That&apos;s the whole list.
              </p>
            </>
          )}
          {botUrl() && (
            <a
              href={botUrl()!}
              target="_blank"
              rel="noopener noreferrer"
              className="inline-block text-sm text-muted-foreground underline underline-offset-4 hover:text-foreground transition-colors"
            >
              Join the Telegram community
            </a>
          )}
        </div>
      </div>
    )
  }

  return (
    <form
      id="waitlist"
      onSubmit={handleSubmit}
      className={cn(
        "flex w-full gap-2",
        variant === "hero"
          ? "max-w-md mx-auto flex-col sm:flex-row sm:flex-wrap"
          : "max-w-sm mx-auto flex-col sm:flex-row sm:flex-wrap",
        className
      )}
    >
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
        className="flex-1"
        required
      />
      <Button type="submit" disabled={status === "submitting"}>
        {status === "submitting" ? "Submitting..." : "Get Early Access"}
      </Button>
      {status === "error" && (
        <p
          className="basis-full text-sm text-destructive"
          role="alert"
          aria-live="assertive"
        >
          {shownMessage}
        </p>
      )}
    </form>
  )
}
