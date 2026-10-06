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
 * Every refusal that can arrive gets its own sentence, one that says what to do
 * next: ask for a new link, sign in as someone else, or check the link was
 * copied whole. The sentences are keyed by the codes that are actually sent
 * (`ACCEPT_REFUSAL_COPY`).
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
          {ACCEPT_REFUSAL_COPY.identity_mismatch}
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

/**
 * A sentence per refusal of the accept call, keyed by the code that arrives. The
 * API's codes are `invitations.REASONS`, and the accept route answers two of
 * them. `invalid_invitation` is the BFF route's own refusal of a malformed token,
 * and `unreachable` is the browser's fetch throwing (`callBff` reports it at
 * status 0). A test reads REASONS from the API's source, so the page cannot keep
 * a sentence for a code nothing sends.
 */
export const ACCEPT_REFUSAL_COPY: Record<string, string> = {
  // The API answers an expired, used or withdrawn invitation alike, and one whose
  // sender or workspace no longer qualifies: one sentence, one remedy.
  not_acceptable:
    "This invitation has expired, has already been used, or was withdrawn. Ask the person who invited you for a new link.",
  identity_mismatch:
    "This invitation is for a different Google account. Sign out, then sign in with the one it was sent to.",
  invalid_invitation: "This link is not a valid invitation. Check you copied all of it.",
  unreachable: "We could not reach Storydump. This one is on us.",
};

function messageFor(reason: unknown, status: number): string {
  if (typeof reason === "string" && Object.hasOwn(ACCEPT_REFUSAL_COPY, reason)) {
    return ACCEPT_REFUSAL_COPY[reason];
  }
  if (status === 503) {
    return "Storydump cannot accept invitations yet. Nothing you did — check back shortly.";
  }
  return "That did not work. This one is on us.";
}
