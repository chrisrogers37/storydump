import Link from "next/link";
import { getSession } from "@/lib/session";
import { AcceptInvitation } from "@/components/workspace/accept-invitation";
import { siteConfig } from "@/config/site";

export const metadata = {
  title: `Join a workspace — ${siteConfig.name}`,
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
    <div className="flex min-h-svh flex-col items-center justify-center bg-background px-4 py-16">
      <div className="w-full max-w-sm space-y-6">
        <div className="space-y-2 text-center">
          <h1 className="text-2xl font-bold tracking-tight">
            You have been invited.
          </h1>
          <p className="text-sm text-muted-foreground">
            {session
              ? "Accept to join the workspace and start posting together."
              : `Sign in to see the invitation and accept it.`}
          </p>
        </div>

        <div className="rounded-lg border bg-card p-6 shadow-sm">
          {session ? (
            <AcceptInvitation token={token} />
          ) : (
            <Link
              href={`/join/${encodeURIComponent(token)}/start`}
              className="inline-flex w-full items-center justify-center rounded-md bg-primary px-4 py-2.5 text-sm font-medium text-primary-foreground transition-colors hover:bg-primary/90"
            >
              Sign in to continue
            </Link>
          )}
        </div>

        {session && (
          <p className="text-center text-xs text-muted-foreground">
            Signed in as {session.displayName || session.email || "you"}.{" "}
            <Link href="/api/auth/logout" className="underline underline-offset-2">
              Use a different account
            </Link>
          </p>
        )}
      </div>
    </div>
  );
}
