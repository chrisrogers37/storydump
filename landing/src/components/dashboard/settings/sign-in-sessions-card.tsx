import { SignOutButton } from "@/components/auth/sign-out-button";
import { buttonVariants } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";

/**
 * Sign out of all devices — the person's, not the workspace's.
 *
 * Every live session of this person is revoked at the API
 * (`sessions.revoke_all_for_user`, `07` §1), this browser's included, so the
 * button lands on `/login` like the header's sign-out. It is the same
 * `SignOutButton` — a button that POSTs, never a link
 * (`signout-never-a-link-contract.test.ts`) — with `everywhere` set.
 *
 * The copy says what happens and nothing more: there is no list of devices
 * here, because the API does not record which device holds which session.
 */
export function SignInSessionsCard() {
  return (
    <Card>
      <CardHeader>
        <CardTitle>Signed-in devices</CardTitle>
      </CardHeader>
      <CardContent className="space-y-4">
        <p className="text-sm text-muted-foreground">
          Signed in on a shared computer, or a phone you no longer have? This
          signs you out of Storydump on every device and browser, including
          this one. Each of them needs to sign in with Google again.
        </p>
        <p className="text-sm text-muted-foreground">
          It ends web sign-ins only. API tokens keep working until you revoke
          them in the API tokens tab, and a linked Telegram account stays
          linked until you unlink it in the Integrations tab.
        </p>
        <SignOutButton everywhere className={buttonVariants({ variant: "outline" })}>
          Sign out of all devices
        </SignOutButton>
      </CardContent>
    </Card>
  );
}
