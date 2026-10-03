import { siteConfig } from "@/config/site";

/**
 * The copy `/login` says things in, split from the page so it is testable
 * without a DOM — the `/auth/error` page's `content.ts` arrangement.
 *
 * The page is prerendered, so a refusal reaches it as `?error=<code>` and a
 * client component reads it. Only sign-up's refusal lands here: every other
 * sign-in failure has its own page at `/auth/error`, which says what went
 * wrong with the attempt. This one is not a failed attempt — the person is
 * simply not in yet — so it belongs next to the button and the waitlist.
 */

/** The home page's waitlist form (`WaitlistLink`'s target off the home page). */
export const WAITLIST_HREF = "/#waitlist";

/** A sentence with one link in it: `lead`, then `link` (to the waitlist), then `tail`. */
export type Linked = { lead: string; link: string; tail: string };

/**
 * `auth.py` sends `error=not_admitted` when a NEW Google account's email was
 * neither admitted by the owner nor invited to a workspace (migration 092).
 * Nothing was created. An existing account never sees it.
 */
export const LOGIN_ERRORS = {
  not_admitted: {
    lead: `${siteConfig.name} is invite-only while in beta. `,
    link: "Join the waitlist",
    tail: " and we’ll email you when your spot is ready.",
  },
} satisfies Record<string, Linked>;

export type LoginError = keyof typeof LOGIN_ERRORS;

/** The copy for `?error=`, or null for none or a code this page does not know. */
export function loginError(code: string | null | undefined): Linked | null {
  return code && Object.hasOwn(LOGIN_ERRORS, code)
    ? LOGIN_ERRORS[code as LoginError]
    : null;
}

/** The line under the card. */
export const NEW_HERE: Linked = {
  lead: `New here? ${siteConfig.name} is invite-only for now. `,
  link: "Join the waitlist",
  tail: ".",
};
