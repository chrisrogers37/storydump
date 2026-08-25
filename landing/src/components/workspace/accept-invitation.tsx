"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";
import { Loader2 } from "lucide-react";

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
  const [error, setError] = useState<string | null>(null);

  async function accept() {
    setPending(true);
    setError(null);
    try {
      const response = await fetch(
        `/api/invitations/${encodeURIComponent(token)}/accept`,
        { method: "POST" },
      );
      if (!response.ok) {
        const body = await response.json().catch(() => ({}));
        setError(messageFor(body?.error, response.status));
        setPending(false);
        return;
      }
      router.push("/dashboard");
      router.refresh();
    } catch {
      setError("We could not reach Storydump. This one is on us.");
      setPending(false);
    }
  }

  return (
    <div className="space-y-3">
      <button
        type="button"
        onClick={accept}
        disabled={pending}
        className="inline-flex w-full items-center justify-center gap-2 rounded-md bg-primary px-4 py-2.5 text-sm font-medium text-primary-foreground transition-colors hover:bg-primary/90 disabled:pointer-events-none disabled:opacity-50"
      >
        {pending && <Loader2 className="h-4 w-4 animate-spin" aria-hidden />}
        {pending ? "Joining…" : "Accept invitation"}
      </button>
      {error && (
        <p role="alert" className="text-sm text-destructive">
          {error}
        </p>
      )}
    </div>
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
    case "email_mismatch":
      return "This invitation was sent to a different address. Sign in with that account.";
    case "already_member":
      return "You are already in this workspace.";
    default:
      if (status === 503) {
        return "Storydump cannot accept invitations yet. Nothing you did — check back shortly.";
      }
      return "That did not work. This one is on us.";
  }
}
