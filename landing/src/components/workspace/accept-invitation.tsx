"use client";

import { useRouter } from "next/navigation";
import { useState, type ReactElement } from "react";
import { Loader2 } from "lucide-react";

import { callBff, postJson } from "@/lib/bff";
import { Button } from "@/components/ui/button";
import { SignOutButton } from "@/components/auth/sign-out-button";

/**
 * The accept control.
 *
 * Every refusal gets its own sentence. "Invalid invitation" would cover expired,
 * revoked, already-accepted and mistyped with one message that tells the reader
 * nothing about whether to ask for another link, sign in as someone else, or
 * simply go to the workspace they are already in.
 */
export function AcceptInvitation({ token }: { token: string }) {
  const router = useRouter();
  const [pending, setPending] = useState(false);
  const [refusal, setRefusal] = useState<{ reason: unknown; status: number } | null>(null);

  async function accept() {
    setPending(true);
    setRefusal(null);
    const result = await callBff(
      `/api/invitations/${encodeURIComponent(token)}/accept`,
      postJson({}),
    );
    if (!result.ok) {
      setRefusal({ reason: result.error, status: result.status });
      setPending(false);
      return;
    }
    router.push("/dashboard");
    router.refresh();
  }

  return (
    <div className="space-y-3">
      <Button
        type="button"
        size="lg"
        className="w-full"
        onClick={accept}
        disabled={pending}
      >
        {pending && <Loader2 className="h-4 w-4 animate-spin" aria-hidden />}
        {pending ? "Joining…" : "Accept invitation"}
      </Button>
      {refusal && <AcceptRefusal reason={refusal.reason} status={refusal.status} token={token} />}
    </div>
  );
}

/**
 * A refused accept: its sentence and, where one exists, the action that resolves
 * it. Hook-free and exported, so a test can read it as an element tree.
 *
 * `identity_mismatch` is the API's answer when the signed-in Google address is
 * not the invited one. Signing in again is the remedy, so the sign-out is right
 * there, landing back on this invitation as the page's own does: only
 * `/join/[token]/start` sets the cookie that carries the invitation through the
 * next sign-in. The invited address is not named, for the reason the page names
 * nothing about the invitation to whoever holds the link.
 */
export function AcceptRefusal({
  reason,
  status,
  token,
}: {
  reason: unknown;
  status: number;
  token: string;
}): ReactElement {
  if (reason === "identity_mismatch") {
    return (
      <div className="space-y-1">
        <p role="alert" className="text-sm text-destructive">
          This invitation is for a different Google account. Sign out, then sign in with the
          one it was sent to.
        </p>
        <SignOutButton
          className="text-sm underline underline-offset-2"
          redirectTo={`/join/${encodeURIComponent(token)}`}
        >
          Use a different account
        </SignOutButton>
      </div>
    );
  }
  return (
    <p role="alert" className="text-sm text-destructive">
      {messageFor(reason, status)}
    </p>
  );
}

function messageFor(reason: unknown, status: number): string {
  switch (reason) {
    case "invitation_expired":
      return "This invitation has expired. Ask for a new one.";
    case "invitation_revoked":
      return "This invitation was withdrawn.";
    case "invitation_accepted":
      return "This invitation has already been used.";
    case "invitation_not_found":
      return "This link is not a valid invitation. Check you copied all of it.";
    case "already_member":
      return "You are already in this workspace.";
    // The browser's own `fetch` throwing. It used to arrive in a `catch` beside
    // this switch with its own sentence; `callBff` reports it as a refusal
    // named `unreachable` at status 0, so the sentence moves in here unchanged.
    case "unreachable":
      return "We could not reach Storydump. This one is on us.";
    default:
      if (status === 503) {
        return "Storydump cannot accept invitations yet. Nothing you did — check back shortly.";
      }
      return "That did not work. This one is on us.";
  }
}
