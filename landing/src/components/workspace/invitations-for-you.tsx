"use client";

import { useState, type ReactElement } from "react";
import { Loader2 } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import {
  askForTheLink,
  invitedLine,
  linkSentAgainCopy,
  sendLinkAgain,
  whereTheLinkIs,
  type MyInvitations,
} from "@/lib/invitations";

/**
 * The invitations waiting for the signed-in person (115), on /welcome
 * and /workspaces: someone who signed in without the link learns one exists.
 *
 * The token is still the only way in, so nothing here joins. Each
 * invitation says where its link is. Where email is set up, "Email me the
 * link again" sends a fresh one to the invited address, which stops the old
 * link working once it has gone; elsewhere the panel says whom to ask.
 */
export function InvitationsForYou({
  mine,
  email,
}: {
  /** Null when they could not be read. */
  mine: MyInvitations | null;
  email: string | null;
}) {
  const [busy, setBusy] = useState<string | null>(null);
  const [said, setSaid] = useState<Record<string, string>>({});

  async function resend(id: string) {
    setBusy(id);
    const answer = await sendLinkAgain(id);
    setSaid((before) => ({ ...before, [id]: linkSentAgainCopy(answer, email) }));
    setBusy(null);
  }

  return (
    <InvitationsForYouList
      mine={mine}
      said={said}
      busy={busy}
      onResend={resend}
    />
  );
}

/**
 * The panel as a pure element tree, so a test can read it: one card per
 * invitation, each with its one control where re-sending can work, or whom
 * to ask where it can't, and the line its last re-send answered. Nothing at
 * all when there are none: the page around it is the person's next step. A
 * read that failed says so, never shown as none.
 */
export function InvitationsForYouList({
  mine,
  said,
  busy,
  onResend,
}: {
  mine: MyInvitations | null;
  said: Record<string, string>;
  busy: string | null;
  onResend: (id: string) => void;
}): ReactElement | null {
  if (mine === null) {
    return (
      <p role="status" className="text-sm text-muted-foreground">
        We couldn&apos;t check for invitations just now.
      </p>
    );
  }
  const { invitations, canResend } = mine;
  if (invitations.length === 0) return null;
  return (
    <section aria-labelledby="invitations-for-you" className="space-y-3">
      <h2 id="invitations-for-you" className="text-sm font-semibold">
        {invitations.length === 1 ? "An invitation for you" : "Invitations for you"}
      </h2>
      <ul className="space-y-3">
        {invitations.map((invitation) => (
          <li key={invitation.id}>
            <Card className="space-y-3 p-4">
              <div className="space-y-1">
                <p className="text-sm font-medium">{invitedLine(invitation)}</p>
                <p className="text-sm text-muted-foreground">
                  {whereTheLinkIs(invitation)}
                </p>
              </div>
              {canResend ? (
              <Button
                type="button"
                variant="outline"
                size="sm"
                disabled={busy !== null}
                onClick={() => onResend(invitation.id)}
              >
                {busy === invitation.id && (
                  <Loader2 className="h-4 w-4 animate-spin" aria-hidden />
                )}
                {busy === invitation.id ? "Sending…" : "Email me the link again"}
              </Button>
              ) : (
                <p className="text-sm">{askForTheLink(invitation)}</p>
              )}
              {said[invitation.id] && (
                <p role="status" className="text-sm">
                  {said[invitation.id]}
                </p>
              )}
            </Card>
          </li>
        ))}
      </ul>
    </section>
  );
}
