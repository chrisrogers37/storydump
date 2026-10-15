import { siteConfig } from "@/config/site";

/**
 * The claim page's copy and its one rule (#1723), split from the page so they
 * are testable without a DOM, as `login/content.ts` is.
 */

/**
 * A claim token's shape, which is the API's (`signup_admissions._TOKEN`, used
 * by `wellformed`): 1 to 256 URL-safe characters. A link of any other shape
 * cannot be a claim, so the page says so rather than offer a sign-in the API
 * would refuse. `page.test.tsx` holds this pattern to the API's.
 */
export const CLAIM_TOKEN = /^[A-Za-z0-9_-]{1,256}$/;

export function isClaimToken(token: string): boolean {
  return CLAIM_TOKEN.test(token);
}

/** A link of a claim's shape: the form that starts the sign-in. */
export const CLAIM_COPY = {
  title: `Your spot on ${siteConfig.name} is ready.`,
  description: "Any Google account works, and the one you choose becomes your sign-in.",
  button: "Continue with Google",
};

/** A link of no claim's shape: no form, and the way to sign in. */
export const INVALID_COPY = {
  title: "This link isn't valid",
  description:
    "It may have been cut short. Open it again from your email, or sign in if you already have an account.",
  link: "Go to sign in",
};
