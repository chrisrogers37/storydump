"use client";

import { useEffect, useId, useRef, useState, type FormEvent, type ReactElement } from "react";
import { useRouter } from "next/navigation";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Notice } from "@/components/ui/notice";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { CopyButton } from "@/components/setup/copy-button";
import { inviteMemberRefusalCopy, submitInviteMember } from "@/lib/command-client";
import { expiryLabel, joinLinkFrom, type PendingInvitation } from "@/lib/invitations";

/**
 * Invite a person from the Members card (#1563), and show the join link once
 * (#1564). Admins and owners only: the page renders this for them alone.
 *
 * ── The link is shown once, and this file is where "once" is enforced ──────
 *
 * The answer to `invite_member` is the only place the link exists in full; the
 * port keeps a hash of its token. So the link lives in `useState` here, in the
 * component that made it, until Done or leaving the page. `router.refresh()`
 * after an invitation re-reads the pending list and keeps client state. The
 * link is never written to storage, analytics or a log, and never put in a
 * URL. The blocks that render it are hook-free and exported, so a test can
 * prove it appears in exactly one place, the copy control.
 */

type MintedInvite = { link: string; email: string };

/** The shown-once block, as `MintedSecretBlock` shows a minted token. */
export function InviteLinkBlock({
  link,
  email,
  onDone,
}: MintedInvite & { onDone: () => void }): ReactElement {
  return (
    <div role="status" className="space-y-2 rounded-md border border-green-200 bg-green-50 p-3">
      <p className="text-sm font-medium text-green-900">Send this link to them. It works once.</p>
      <CopyButton value={link} className="w-full" />
      <p className="text-xs text-green-900">They need to sign in with Google as {email}.</p>
      <Button type="button" variant="ghost" size="sm" onClick={onDone}>
        Done
      </Button>
    </div>
  );
}

/** The link slot: the block while a link is in state, nothing otherwise. */
export function inviteLinkSlot(
  minted: MintedInvite | null,
  onDone: () => void,
): ReactElement | null {
  return minted ? <InviteLinkBlock link={minted.link} email={minted.email} onDone={onDone} /> : null;
}

/**
 * The invitations not yet accepted. `null` is a read that failed and says so;
 * an empty list renders nothing. A Telegram invitation names no address.
 */
export function PendingInvitations({
  invitations,
  tz,
}: {
  invitations: PendingInvitation[] | null;
  tz: string;
}): ReactElement | null {
  if (invitations === null) {
    return (
      <p className="text-xs text-muted-foreground">
        Pending invitations could not be loaded just now. Reload to try again.
      </p>
    );
  }
  if (invitations.length === 0) return null;
  return (
    <div>
      <p className="text-xs font-medium text-muted-foreground">Pending invitations</p>
      <ul className="divide-y">
        {invitations.map((invitation) => (
          <li key={invitation.id} className="py-2">
            <p className="truncate text-sm">{invitation.email ?? "Telegram invitation"}</p>
            <p className="text-xs text-muted-foreground">
              {`Invited as ${invitation.role} · expires ${expiryLabel(invitation.expiresAt, tz)}`}
            </p>
          </li>
        ))}
      </ul>
    </div>
  );
}

export function InviteMember({
  workspaceId,
  invitations,
  tz,
}: {
  workspaceId: string;
  invitations: PendingInvitation[] | null;
  /** The workspace's zone, for the expiry dates. */
  tz: string;
}) {
  const router = useRouter();
  const ids = useId();
  const [email, setEmail] = useState("");
  const [role, setRole] = useState<"member" | "admin">("member");
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [minted, setMinted] = useState<MintedInvite | null>(null);
  const blockRef = useRef<HTMLDivElement>(null);
  const emailRef = useRef<HTMLInputElement>(null);
  const returnFocus = useRef(false);

  // The form unmounts when the link appears and comes back on Done, so focus
  // is moved by hand both ways rather than left to fall to <body>.
  useEffect(() => {
    if (minted) {
      blockRef.current?.focus();
    } else if (returnFocus.current) {
      returnFocus.current = false;
      emailRef.current?.focus();
    }
  }, [minted]);

  async function invite(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const address = email.trim();
    if (address === "" || pending) return;
    setError(null);
    setPending(true);
    try {
      const result = await submitInviteMember(workspaceId, { email: address, role });
      if (!result.ok) {
        setError(inviteMemberRefusalCopy(result.error, result.status));
        return;
      }
      const link = joinLinkFrom(result.data);
      if (link === null) {
        setError("The invitation was made, but its link can't be built on this deployment.");
      } else {
        setMinted({ link, email: address });
        setEmail("");
        // Back to the least role, as the token form starts read-only: the next
        // invitation is never an admin because the select was left alone.
        setRole("member");
      }
      router.refresh();
    } finally {
      setPending(false);
    }
  }

  function done() {
    returnFocus.current = true;
    setMinted(null);
  }

  return (
    <div className="space-y-3 border-b pb-4">
      {minted ? (
        <div ref={blockRef} tabIndex={-1} className="outline-hidden">
          {inviteLinkSlot(minted, done)}
        </div>
      ) : (
        <form onSubmit={invite} className="space-y-2">
          <Label htmlFor={`${ids}-email`}>Invite by email</Label>
          <div className="flex flex-wrap gap-2">
            <Input
              ref={emailRef}
              id={`${ids}-email`}
              type="email"
              required
              autoComplete="off"
              placeholder="name@example.com"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              className="min-w-48 flex-1"
            />
            <Select value={role} onValueChange={(v) => setRole(v === "admin" ? "admin" : "member")}>
              <SelectTrigger aria-label="Role" className="w-28">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="member">Member</SelectItem>
                <SelectItem value="admin">Admin</SelectItem>
              </SelectContent>
            </Select>
            <Button type="submit" disabled={pending || email.trim() === ""}>
              {pending ? "Inviting…" : "Invite"}
            </Button>
          </div>
        </form>
      )}
      {error && <Notice tone="error">{error}</Notice>}
      <PendingInvitations invitations={invitations} tz={tz} />
    </div>
  );
}
