/**
 * The `.tsx` half of the glob fix — and a real test, not a placeholder.
 *
 * `GoogleLoginButton` returns `null` rather than a disabled control when
 * sign-in cannot complete, so the closed case is assertable WITHOUT a DOM:
 * calling the component returns null outright. That keeps `environment: "node"`
 * honest — jsdom is a separate decision for whenever a test genuinely renders.
 *
 * This file existing under a `.tsx` extension is also what makes
 * `vitest-config.test.ts`'s assertion about something real rather than
 * hypothetical.
 */

import { beforeEach, describe, expect, it } from "vitest";
import { GoogleLoginButton } from "./google-login-button";

beforeEach(() => {
  delete process.env.GOOGLE_CLIENT_ID;
  delete process.env.GOOGLE_CLIENT_SECRET;
  delete process.env.GOOGLE_SIGNIN_REDIRECT_BASE;
});

describe("GoogleLoginButton", () => {
  it("renders nothing while sign-in cannot complete", () => {
    // Nothing configured, storage boundary closed: null, not a disabled button.
    // An unavailable button still says "this is how you sign in", which is the
    // wrong thing to tell someone whose only working option is above it.
    expect(GoogleLoginButton({ origin: "https://storydump.app" })).toBeNull();
  });

  it("renders nothing on an unregistered origin even when configured", () => {
    process.env.GOOGLE_CLIENT_ID = "test-client-id.apps.googleusercontent.com";
    process.env.GOOGLE_CLIENT_SECRET = "test-secret-not-a-real-one";
    process.env.GOOGLE_SIGNIN_REDIRECT_BASE = "https://storydump.app";
    expect(
      GoogleLoginButton({
        origin: "https://storydump-git-some-branch-chrisrogers37.vercel.app",
      }),
    ).toBeNull();
  });

  it("renders nothing when handed no origin at all", () => {
    expect(GoogleLoginButton({ origin: null })).toBeNull();
  });
});
