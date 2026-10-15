import Link from "next/link";
import { GoogleMark, googleButtonClass } from "@/components/auth/google-login-button";
import { buttonVariants } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { PageHeader } from "@/design/page-header";
import { Screen } from "@/design/screen";
import { noindexMetadata } from "@/lib/seo";
import { TARGET_API_URL } from "@/lib/target-api";
import { CLAIM_COPY, INVALID_COPY, isClaimToken } from "./content";

export const metadata = {
  title: "Claim your spot",
  ...noindexMetadata,
};

/**
 * A waitlist admission's emailed link (#1723): one spot, claimable by any
 * Google account, once.
 *
 * ── It reads nothing ───────────────────────────────────────────────────────
 *
 * The page asks the API nothing about the token, so opening it spends nothing
 * and reveals nothing: a mail scanner that fetches every link in an email
 * leaves the claim as it was. Only the form's POST followed by a completed
 * Google sign-in claims, and the API decides it, at the sign-in callback.
 *
 * ── The token goes in a form, never a URL ──────────────────────────────────
 *
 * "Continue with Google" posts the token to the API (`POST /auth/google/claim`),
 * which keeps it in a cookie for the callback and sends the browser on to
 * Google. A query string would leave a live link in the API's access log. The
 * CSP's `form-action` admits the API and Google's sign-in for exactly this
 * post and its redirect (`lib/csp.ts`).
 *
 * Rendered per request, under middleware's nonce policy.
 */
export default async function ClaimPage({
  params,
}: {
  params: Promise<{ token: string }>;
}) {
  const { token } = await params;
  const valid = isClaimToken(token);
  const copy = valid ? CLAIM_COPY : INVALID_COPY;

  return (
    <Screen>
      <PageHeader align="center" title={copy.title} description={copy.description} />
      <Card className="p-6">
        {valid ? (
          <form method="post" action={`${TARGET_API_URL}/auth/google/claim`}>
            <input type="hidden" name="claim" value={token} />
            <button type="submit" className={googleButtonClass}>
              <GoogleMark />
              {CLAIM_COPY.button}
            </button>
          </form>
        ) : (
          <Link href="/login" className={buttonVariants({ size: "lg", className: "w-full" })}>
            {INVALID_COPY.link}
          </Link>
        )}
      </Card>
    </Screen>
  );
}
