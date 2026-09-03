/**
 * #1090 B3: a declined Drive grant rendered "Sign-in was cancelled."
 *
 * `auth.py` has always sent `flow=drive` on the Drive leg — its docstring even
 * says the page "needs to know which leg it renders for" — and the page typed
 * `{ reason?: string }` and never read it. The result was a false statement
 * about the reader's session at the moment they were working out what went
 * wrong, with a CTA sending them to /login to fix a Drive problem.
 *
 * These assert PROPERTIES of the mapping, not the wording. The wording will be
 * edited; the properties are what must survive the edit — and a test that
 * pinned exact strings would pass for any confidently wrong replacement, which
 * is the trap in repairing a file whose job is explaining failures.
 */

import { describe, expect, it } from "vitest";
import { DRIVE, INSTAGRAM, SIGNIN, resolveContent, resolveFlow } from "./content";

const DRIVE_REASONS = [
  "denied",
  "missing_params",
  "state_refused",
  "exchange_failed",
  "grant_incomplete",
] as const;

describe("the error page knows which leg it is rendering for", () => {
  it("routes a Drive failure to the Drive table", () => {
    expect(resolveFlow("drive")).toBe("drive");
    expect(resolveFlow(undefined)).toBe("signin");
    expect(resolveFlow("something-else")).toBe("signin");
  });

  // THROUGH `resolveContent`, NEVER over the table directly — and that is the
  // difference between a test that catches this defect and one that does not.
  // Mutation-checked: with `flow` ignored (the original bug) the DRIVE table
  // is still perfectly correct, it is simply never reached. Asserting over
  // `DRIVE` therefore passes while the defect is live. These go through the
  // resolution because the resolution is what was broken.
  const asRendered = (reason: string) => resolveContent("drive", reason);

  it("never tells a Drive user anything about their sign-in or session", () => {
    // They are signed in — that is how they started the connect — so any
    // claim about the session is false, and the remedy it implies
    // (re-authenticate) abandons what they were doing.
    for (const reason of [...DRIVE_REASONS, "unrecognised"]) {
      const c = asRendered(reason);
      const prose = `${c.heading} ${c.body}`.toLowerCase();
      expect(prose, reason).not.toContain("sign-in");
      expect(prose, reason).not.toContain("sign in");
      expect(prose, reason).not.toContain("signed out");
      expect(prose, reason).not.toContain("session");
    }
  });

  it("never sends a Drive user to /login", () => {
    for (const reason of [...DRIVE_REASONS, "unrecognised"]) {
      expect(asRendered(reason).href, reason).not.toBe("/login");
    }
  });

  it("covers every reason the Drive leg can send, without falling to generic", () => {
    // `grant_incomplete` was absent from the vocabulary entirely, so it landed
    // on a generic that also said "sign you in". The page's own comment
    // predicted this ("if the API adds a sixth, it lands here as the
    // fallback") — the sixth already existed.
    for (const reason of DRIVE_REASONS) {
      expect(
        resolveContent("drive", reason),
        `${reason} fell through to the generic fallback`,
      ).not.toBe(DRIVE.generic);
    }
  });

  it("still renders sign-in copy for the sign-in leg", () => {
    // The positive control. A test that only ever checks the Drive table
    // cannot tell per-leg routing from "everything is Drive now".
    const content = resolveContent("signin", "denied");
    expect(content).toBe(SIGNIN.denied);
    expect(`${content.heading} ${content.body}`.toLowerCase()).toContain("sign");
  });

  it("falls back within the leg, never across it", () => {
    // An unknown reason on the Drive leg must get the DRIVE generic — the
    // old page had one table, so any fallback was a sign-in fallback.
    expect(resolveContent("drive", "not_a_real_reason")).toBe(DRIVE.generic);
    expect(resolveContent("signin", "not_a_real_reason")).toBe(SIGNIN.generic);
  });

  it("does not name a single cause for a reason that has two", () => {
    // `denied` is reached when the person declined AND when Google refused
    // (#1116). The page cannot distinguish them, so the copy must not assert
    // one. Checked as: it does not claim the reader performed the action.
    const prose = `${DRIVE.denied.heading} ${DRIVE.denied.body}`.toLowerCase();
    expect(prose).not.toMatch(/you closed/);
    expect(prose).toContain("google");
  });
});

describe("the Instagram connect leg has its own table", () => {
  it("routes flow=instagram to the Instagram table", () => {
    expect(resolveFlow("instagram")).toBe("instagram");
    expect(resolveContent("instagram", "denied")).toBe(INSTAGRAM.denied);
  });

  it("covers every reason the Instagram leg can send, without falling to generic", () => {
    for (const reason of ["denied", "missing_params", "state_refused", "exchange_failed", "already_connected"]) {
      expect(resolveContent("instagram", reason)).not.toBe(INSTAGRAM.generic);
    }
  });

  it("never tells an Instagram user anything about their sign-in or session, and returns them to Settings", () => {
    for (const content of Object.values(INSTAGRAM) as { heading: string; body: string; href: string }[]) {
      expect(`${content.heading} ${content.body}`).not.toMatch(/sign[- ]in|session|signed out/i);
      expect(content.href).toBe("/dashboard/settings");
    }
  });

  it("says why on already_connected, because retrying identically reproduces it", () => {
    expect(INSTAGRAM.already_connected.body).toMatch(/already/i);
  });
});
