/**
 * The `.tsx` half of the glob fix — and a real test, not a placeholder.
 *
 * REWRITTEN, because the contract it pinned no longer exists. The button used
 * to run the OIDC flow in this tier and return `null` when it could not
 * complete; the API hosts sign-in now, so there is no local availability
 * question left to assert and no closed case to render. What replaced it is a
 * single anchor, which is still assertable WITHOUT a DOM by reading the
 * returned element's props — so `environment: "node"` stays honest.
 *
 * This file existing under a `.tsx` extension is also what makes
 * `vitest-config.test.ts`'s assertion about something real rather than
 * hypothetical.
 */

import { describe, expect, it } from "vitest";
import type { ReactElement } from "react";
import { GoogleLoginButton } from "./google-login-button";

/** The rendered anchor's props, without a DOM. */
function anchor(): { href: string; className: string } {
  const el = GoogleLoginButton() as ReactElement<{
    href: string;
    className: string;
  }>;
  return el.props;
}

describe("GoogleLoginButton", () => {
  it("links to the API's sign-in endpoint, not to a route in this tier", () => {
    // The whole point of the collapse. A path under this app's own origin
    // would mean the BFF was still running a second OIDC flow.
    expect(anchor().href).toMatch(/\/auth\/google$/);
    expect(anchor().href).not.toMatch(/\/api\//);
  });

  it("renders unconditionally — availability is the API's question now", () => {
    // Deliberately asserted with NOTHING configured in this tier. The previous
    // button read Google's client id and secret from this environment and
    // rendered null without them; keeping that check would have meant a second
    // copy of the API's configuration, which goes stale silently.
    delete process.env.GOOGLE_CLIENT_ID;
    delete process.env.GOOGLE_CLIENT_SECRET;
    delete process.env.GOOGLE_SIGNIN_REDIRECT_BASE;
    expect(GoogleLoginButton()).not.toBeNull();
    expect(anchor().href).toMatch(/\/auth\/google$/);
  });

  it("carries a visible focus ring", () => {
    // It is the only control on the sign-in page, so it is the only thing a
    // keyboard user can land on. A missing ring here is a dead end, not a nit.
    expect(anchor().className).toContain("focus-visible:ring-2");
  });
});
