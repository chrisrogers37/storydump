import { getSession } from "@/lib/session";
import { AcceptInvitation } from "@/components/workspace/accept-invitation";
import { SignOutButton } from "@/components/auth/sign-out-button";
import { noindexMetadata } from "@/lib/seo";
import { buttonVariants } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { PageHeader } from "@/design/page-header";
import { Screen } from "@/design/screen";

export const metadata = {
  title: "Join a workspace",
  ...noindexMetadata,
};

/**
 * Accept an invitation.
 *
 * ── The invitation is NOT described before sign-in ─────────────────────────
 *
 * This page could look up the workspace name from the token and greet an
 * anonymous visitor with "Join Northside Coffee". It deliberately does not.
 * `workspace_invitations.token_hash` is a bearer credential that arrives by
 * email or Telegram, and a link that leaks a company's internal workspace name
 * to anyone who receives, forwards or intercepts it is a disclosure with no
 * matching benefit — the person who was actually invited already knows where
 * they are being invited.
 *
 * So the anonymous shape says only that an invitation exists. Everything about
 * the workspace appears after sign-in, to a named user, on the accept screen.
 *
 * ── Validity is not checked here either ────────────────────────────────────
 *
 * An expired, revoked or already-accepted invitation is refused at the accept
 * call, not on load. Checking on load would let an anonymous visitor probe
 * tokens for validity one page-load at a time, and would say nothing this
 * screen can act on.
 */
export default async function JoinPage({
  params,
}: {
  params: Promise<{ token: string }>;
}) {
  const { token } = await params;
  const session = await getSession().catch(() => null);

  return (
    <Screen>
      <PageHeader
        align="center"
        title="You’re invited."
        description={
          session
            ? "Accept to join the workspace and start posting together."
            : "Sign in to see the invitation and accept it."
        }
      />

      <Card className="p-6">
        {session ? (
          <AcceptInvitation token={token} />
        ) : (
          // An anchor, not <Link>: `start` is a route handler, which a Link
          // prefetch would run on every view of this page.
          <a
            href={`/join/${encodeURIComponent(token)}/start`}
            className={buttonVariants({ size: "lg", className: "w-full" })}
          >
            Sign in to continue
          </a>
        )}
      </Card>

      {session && (
        <p className="text-center text-xs text-muted-foreground">
          Signed in as {session.displayName || session.email || "you"}.{" "}
          <SignOutButton
            className="underline underline-offset-2"
            redirectTo={`/join/${encodeURIComponent(token)}`}
          >
            Use a different account
          </SignOutButton>
        </p>
      )}
    </Screen>
  );
}
