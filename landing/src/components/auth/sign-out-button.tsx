"use client";

import { useRouter } from "next/navigation";
import type { ReactNode } from "react";

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
}) {
  const router = useRouter();

  async function signOut() {
    await fetch("/api/auth/logout", { method: "POST" });
    router.push(redirectTo);
    // Needed when `redirectTo` IS the current route, which is the invitation
    // page's case: a push to the URL already showing renders from the router
    // cache and would re-display the signed-in view of a session that no
    // longer exists. Harmless for the callers that navigate away.
    router.refresh();
  }

  return (
    <button type="button" onClick={signOut} className={className}>
      {children}
    </button>
  );
}
