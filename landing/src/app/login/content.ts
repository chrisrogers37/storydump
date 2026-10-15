import { siteConfig } from "@/config/site";

/**
 * The copy `/login` says things in, split from the page so it is testable
 * without a DOM — the `/auth/error` page's `content.ts` arrangement.
 *
 * The page is prerendered, so a refusal reaches it as `?error=<code>` and a
 * client component reads it. Only sign-up's refusals land here: every other
 * sign-in failure has its own page at `/auth/error`, which says what went
 * wrong with the attempt. These are not failed attempts — the person is
 * simply not in yet — so they belong next to the button and the waitlist.
 */

/** The home page's waitlist form. A plain link: /login sends no analytics event. */
export const WAITLIST_HREF = "/#waitlist";

/** A sentence with one link in it: `lead`, then `link` (to the waitlist), then `tail`. */
export type Linked = { lead: string; link: string; tail: string };

/**
 * `auth.py` sends `error=not_admitted` when a NEW Google account's email was
 * neither admitted by the owner nor invited to a workspace (`fn_signup_admitted`),
 * and `error=claim_refused` when such an account came through a claim link
 * from an admission's email that cannot be used: used already, replaced by a
 * newer email, or its spot taken (signup admission functions). Nothing was created either way,
 * and an existing account sees neither.
 */
export const LOGIN_ERRORS = {
  not_admitted: {
    lead: `${siteConfig.name} is invite-only while in beta. `,
    link: "Join the waitlist",
    tail:
      " and we’ll email you when your spot is ready. Invited by a teammate? Sign in with the Google account your invite went to.",
  },
  claim_refused: {
    lead:
      "That link was already used, or a newer email replaced it. Already signed up? Sign in with the Google account you chose. Otherwise, use the link in your newest email, or ",
    link: "join the waitlist",
    tail: ".",
  },
} satisfies Record<string, Linked>;

export type LoginError = keyof typeof LOGIN_ERRORS;

/** The copy for `?error=`, or null for none or a code this page does not know. */
export function loginError(code: string | null | undefined): Linked | null {
  return pick(LOGIN_ERRORS, code);
}

/** `map[code]` for a code the map owns, else null (never an inherited key). */
function pick<T>(map: Record<string, T>, code: string | null | undefined): T | null {
  return code && Object.hasOwn(map, code) ? map[code] : null;
}

/**
 * `?signout=` from the sign-out route, when the server side of a sign-out did
 * not fully happen. This browser is signed out in every case.
 * - `unconfirmed`: a plain sign-out the API never confirmed.
 * - `incomplete`: "Sign out of all devices" could not reach the API.
 * - `stale`: "Sign out of all devices" from a browser whose session had
 * already ended, so there was no session to say whose devices to sign out.
 */
export const SIGNOUT_NOTICES = {
  unconfirmed:
    "This browser is signed out, but we couldn't confirm your session ended. On a shared computer, sign in and use Sign out of all devices.",
  incomplete:
    "This browser is signed out, but we couldn't sign out your other devices. Sign in and try Sign out of all devices again.",
  stale:
    "This browser was already signed out, so your other devices weren't. Sign in and try Sign out of all devices again.",
} satisfies Record<string, string>;

/** The copy for `?signout=`, or null for none or a value this page does not know. */
export function signoutNotice(code: string | null | undefined): string | null {
  return pick(SIGNOUT_NOTICES, code);
}

/** The line under the card. */
export const NEW_HERE: Linked = {
  lead: `New here? ${siteConfig.name} is invite-only for now. `,
  link: "Join the waitlist",
  tail: ".",
};
