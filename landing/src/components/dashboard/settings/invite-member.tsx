"use client";

import {
  useEffect,
  useId,
  useRef,
  useState,
  useTransition,
  type FormEvent,
  type ReactElement,
} from "react";
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
import {
  inviteMemberRefusalCopy,
  revokeEndedIt,
  revokeInvitationRefusalCopy,
  submitInviteMember,
  submitRevokeInvitation,
} from "@/lib/command-client";
import {
  expiryLabel,
  inviteLinkHeadline,
  mintedInviteFrom,
  type MintedInvite,
  type PendingInvitation,
} from "@/lib/invitations";

/**
 * Invite a person from the Members card (#1563), and show the join link once
 * (#1564). Admins and owners only: the page renders this for them alone.
 *
 * ── The link is shown once, and this file is where "once" is enforced ──────
 *
 * The answer to `invite_member` is the only place the link exists in full; the
 * port keeps a hash of its token. So the link lives in `useState` here, in the
 * component that made it, until Done, the next invitation, its Revoke, or
 * leaving the page. `router.refresh()` after an invitation re-reads the
 * pending list and keeps client state. The link is never written to storage,
 * analytics or a log, and never put in a URL. The blocks that render it are
 * hook-free and exported, so a test can prove it appears in exactly one place,
 * the copy control.
 */

/**
 * The shown-once block, as `MintedSecretBlock` shows a minted token. Its first
 * line says whether the invitation was emailed, so the inviter knows how much
 * the link on screen matters.
 */
export function InviteLinkBlock({
  link,
  email,
  delivery,
  onDone,
}: MintedInvite & { onDone: () => void }): ReactElement {
  return (
    <div role="status" className="space-y-2 rounded-md border border-green-200 bg-green-50 p-3">
      <p className="text-sm font-medium text-green-900">{inviteLinkHeadline(delivery, email)}</p>
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
  return minted ? <InviteLinkBlock {...minted} onDone={onDone} /> : null;
}

/**
 * What is in flight: a command, with the row it acts on, or the page re-reading
 * the list after one. Until that re-read lands, a row on screen may be one the
 * command just ended, so every control waits for both.
 */
export type InFlight = { action: "invite" | "resend" | "revoke" | "refresh"; id?: string };

/**
 * The invitations not yet accepted. `null` is a read that failed and says so;
 * an empty list renders nothing. A Telegram invitation names no address, so it
 * can be revoked but not resent: Resend is a new email invitation to the same
 * address, and the old link stops working.
 */
export function PendingInvitations({
  invitations,
  tz,
  onResend,
  onRevoke,
  busy,
}: {
  invitations: PendingInvitation[] | null;
  tz: string;
  onResend: (invitation: PendingInvitation) => void;
  onRevoke: (invitation: PendingInvitation) => void;
  busy: InFlight | null;
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
        {invitations.map((invitation) => {
          const rowBusy = busy?.id === invitation.id ? busy.action : null;
          return (
            <li key={invitation.id} className="flex items-center justify-between gap-2 py-2">
              <div className="min-w-0">
                <p className="truncate text-sm">{invitation.email ?? "Telegram invitation"}</p>
                <p className="text-xs text-muted-foreground">
                  {`Invited as ${invitation.role} · expires ${expiryLabel(invitation.expiresAt, tz)}`}
                </p>
              </div>
              <div className="flex shrink-0 gap-2">
                {invitation.email !== null && (
                  <Button
                    variant="outline"
                    size="sm"
                    disabled={busy !== null}
                    onClick={() => onResend(invitation)}
                  >
                    {rowBusy === "resend" ? "Resending…" : "Resend"}
                  </Button>
                )}
                <Button
                  variant="outline"
                  size="sm"
                  disabled={busy !== null}
                  onClick={() => onRevoke(invitation)}
                >
                  {rowBusy === "revoke" ? "Revoking…" : "Revoke"}
                </Button>
              </div>
            </li>
          );
        })}
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
  const [inFlight, setInFlight] = useState<InFlight | null>(null);
  const [refreshing, startRefresh] = useTransition();
  const [error, setError] = useState<string | null>(null);
  const [minted, setMinted] = useState<MintedInvite | null>(null);
  const blockRef = useRef<HTMLDivElement>(null);
  const emailRef = useRef<HTMLInputElement>(null);
  const returnFocus = useRef(false);
  const busy: InFlight | null = inFlight ?? (refreshing ? { action: "refresh" } : null);

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

  /** One action at a time, whichever control started it. */
  async function run(next: InFlight, work: () => Promise<void>) {
    if (busy) return;
    setError(null);
    setInFlight(next);
    try {
      await work();
    } finally {
      setInFlight(null);
    }
  }

  function refresh() {
    startRefresh(() => router.refresh());
  }

  /** One invitation, from the form or a row's Resend; true when a link is shown. */
  async function sendInvite(address: string, inviteRole: string): Promise<boolean> {
    const result = await submitInviteMember(workspaceId, { email: address, role: inviteRole });
    // Re-read the list whatever the answer: a lost one may still have made it.
    refresh();
    if (!result.ok) {
      setError(inviteMemberRefusalCopy(result.error, result.status));
      return false;
    }
    const made = mintedInviteFrom(result.data, address);
    if (made === null) {
      setError("The invitation was made, but its link can't be built on this deployment.");
    } else {
      setMinted(made);
    }
    return made !== null;
  }

  async function invite(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const address = email.trim();
    if (address === "") return;
    await run({ action: "invite" }, async () => {
      if (await sendInvite(address, role)) {
        setEmail("");
        // Back to the least role, as the token form starts read-only: the next
        // invitation is never an admin because the select was left alone.
        setRole("member");
      }
    });
  }

  async function resend(invitation: PendingInvitation) {
    const address = invitation.email;
    if (address === null) return;
    // The row's own role, unmapped: the port owns which roles are invitable.
    await run({ action: "resend", id: invitation.id }, async () => {
      await sendInvite(address, invitation.role);
    });
  }

  async function revoke(invitation: PendingInvitation) {
    await run({ action: "revoke", id: invitation.id }, async () => {
      const result = await submitRevokeInvitation(workspaceId, invitation.id);
      // Re-read the list whatever the answer: an unconfirmed one may still have revoked it.
      refresh();
      if (!result.ok) setError(revokeInvitationRefusalCopy(result.error, result.status));
      if (minted?.id === invitation.id && revokeEndedIt(result)) setMinted(null);
    });
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
            <Button type="submit" disabled={busy !== null || email.trim() === ""}>
              {busy?.action === "invite" ? "Inviting…" : "Invite"}
            </Button>
          </div>
        </form>
      )}
      {error && <Notice tone="error">{error}</Notice>}
      <PendingInvitations
        invitations={invitations}
        tz={tz}
        onResend={resend}
        onRevoke={revoke}
        busy={busy}
      />
    </div>
  );
}
