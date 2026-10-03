"use client";

import { useRouter } from "next/navigation";
import { useState, type ReactNode } from "react";

/** What the button says after a sign-out that did not happen. */
export const SIGNOUT_FAILED = "Couldn't sign out. Try again";

/**
 * Where a sign-out landed. `failed` means this browser is still signed in:
 * the request never arrived (offline) or the route refused it (a 403 from the
 * cross-site check, a 5xx), so its cookies were not cleared. `incomplete`
 * means this browser is signed out but the API could not sign the other
 * devices out; the route says so by redirecting to `/login?signout=incomplete`.
 */
export function signOutOutcome(
  response: Pick<Response, "ok" | "redirected" | "url"> | null,
): "done" | "incomplete" | "failed" {
  if (!response || !response.ok || !response.redirected) return "failed";
  const landed = new URL(response.url);
  return landed.searchParams.get("signout") === "incomplete" ? "incomplete" : "done";
}

/**
 * Sign out.
 *
 * A BUTTON THAT POSTS, NEVER A LINK. Sign-out revokes
 * `session_tokens.revoked_at` server-side, so it mutates state — and anything
 * that speculatively fetches a URL will perform it without a person: Next's
 * own `<Link>` prefetch, crawlers, link previewers, chat unfurlers, email
 * clients. `/welcome` linked here with `next/link` and every session died
 * about a second after it was minted, to a `GET …/api/auth/logout?_rsc=…`
 * nobody clicked.
 *
 * The route is POST-only for the same reason. This component exists so the two
 * places that offer sign-out cannot drift apart again — the divergence is what
 * produced the defect, not either version on its own.
 */
export function SignOutButton({
  className,
  children = "Sign out",
  redirectTo = "/login",
  everywhere = false,
}: {
  className?: string;
  /** The label. Defaults to "Sign out"; the invitation page offers the same
   *  action as "Use a different account", which is what it means there. */
  children?: ReactNode;
  /**
   * Where to land afterwards. `/login` for the dashboard and `/welcome`.
   *
   * The invitation page passes its OWN url, and that is not cosmetic: the
   * invite token survives sign-in in a 15-minute httpOnly cookie that ONLY
   * `/join/[token]/start` sets. Landing on `/login` therefore depends on a
   * cookie that may already have expired, and a person who signs in as
   * somebody else is stranded at `/welcome` with the invitation lost.
   * Returning to the invitation re-enters the flow that mints a fresh one.
   */
  redirectTo?: string;
  /**
   * Sign out of EVERY device, not only this browser: the API revokes every
   * live session of this person (`?everywhere=1` on the route). Settings ›
   * General offers it; the header's plain sign-out never does.
   */
  everywhere?: boolean;
}) {
  const router = useRouter();
  const [failed, setFailed] = useState(false);

  async function signOut() {
    let response: Response | null = null;
    try {
      response = await fetch(
        everywhere ? "/api/auth/logout?everywhere=1" : "/api/auth/logout",
        { method: "POST" },
      );
    } catch {
      // Offline, or the request was cut off: nothing was signed out.
    }
    const outcome = signOutOutcome(response);
    // Stay put and say so: the session is still live, so landing on /login
    // would read as a sign-out that did not happen.
    setFailed(outcome === "failed");
    if (outcome === "failed") return;
    // `/login?signout=incomplete` says the other devices are still signed in.
    router.push(outcome === "incomplete" ? "/login?signout=incomplete" : redirectTo);
    // Needed when `redirectTo` IS the current route, which is the invitation
    // page's case: a push to the URL already showing renders from the router
    // cache and would re-display the signed-in view of a session that no
    // longer exists. Harmless for the callers that navigate away.
    router.refresh();
  }

  return (
    <button type="button" onClick={signOut} className={className} aria-live="polite">
      {failed ? SIGNOUT_FAILED : children}
    </button>
  );
}
