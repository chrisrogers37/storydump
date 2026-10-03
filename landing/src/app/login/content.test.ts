import { describe, expect, it } from "vitest";
import { LOGIN_ERRORS, NEW_HERE, loginError } from "./content";

const sentence = (c: { lead: string; link: string; tail: string }) =>
  `${c.lead}${c.link}${c.tail}`;

/**
 * Sign-up is gated (migration 092): a new account nobody admitted or invited
 * lands on `/login?error=not_admitted`. The page must say why, and send the
 * person to the waitlist rather than back to a button that will refuse them
 * again.
 */
describe("the sign-in page explains the sign-up gate", () => {
  it("names the refusal and points at the waitlist", () => {
    const copy = loginError("not_admitted");
    expect(copy).toBe(LOGIN_ERRORS.not_admitted);
    expect(sentence(copy!)).toBe(
      "Storydump is invite-only while in beta. Join the waitlist and we’ll email you when your spot is ready. Invited by a teammate? Sign in with the Google account your invite went to.",
    );
    expect(copy!.link).toBe("Join the waitlist");
  });

  it("shows nothing without a code, or for one it does not know", () => {
    expect(loginError(null)).toBeNull();
    expect(loginError(undefined)).toBeNull();
    expect(loginError("")).toBeNull();
    expect(loginError("identity_collision")).toBeNull();
    // an inherited property is not a code
    expect(loginError("toString")).toBeNull();
  });

  it("no longer tells a newcomer that signing in creates their account", () => {
    expect(sentence(NEW_HERE)).toBe(
      "New here? Storydump is invite-only for now. Join the waitlist.",
    );
    expect(sentence(NEW_HERE).toLowerCase()).not.toContain("creates your account");
  });
});
