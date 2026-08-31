"use client";

import { useRouter } from "next/navigation";

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
export function SignOutButton({ className }: { className?: string }) {
  const router = useRouter();

  async function signOut() {
    await fetch("/api/auth/logout", { method: "POST" });
    router.push("/login");
  }

  return (
    <button type="button" onClick={signOut} className={className}>
      Sign out
    </button>
  );
}
