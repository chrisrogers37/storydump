"use client";

import { useRef, useState } from "react";
import { useRouter } from "next/navigation";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { TONE_CLASS } from "@/components/dashboard/tone";
import { Notice } from "@/components/ui/notice";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
  DialogClose,
} from "@/components/ui/dialog";
import { disableAccountRefusalCopy, submitDisableAccount } from "@/lib/command-client";
import {
  beginConnect,
  confirmConnect,
  connectRefusalCopy,
  type ConnectTarget,
} from "@/lib/connect-choice";
import {
  connectControlFor,
  destinationConnectionCaption,
  destinationHandle,
  destinationName,
  destinationStateBadge,
} from "@/lib/destination";
import type { Destination } from "@/lib/types";
import { ConnectChoiceDialog, type ConnectChoiceState } from "./connect-choice-dialog";

/**
 * Connect is real and ungated: the header's *Connect Instagram* ADDS a
 * destination through the Instagram Login grant,
 * each row's Connect/Reconnect acts on the account it names, and Remove is
 * the port's `disable_account`.
 *
 * THIS SCREEN HAS NO SWITCHING. `COMMAND_SPECS` has no entry for it, and a
 * control that cannot be pressed is not a promise, it is furniture.
 * Switching needs a `switch_account` row in `lib/commands.ts` and a button
 * that calls `submitCommand`, like every other write on this tab.
 *
 * EVERY CONNECT ASKS FIRST (#1644). Add and a row's Connect read the
 * connect preview; where past posts are a choice they open the dialog over
 * it, and everywhere else — past posts no choice, or a preview that cannot be
 * had, as when this tier is deployed ahead of its API — the start goes ahead
 * without the dialog (`connect-choice.ts`).
 */

interface AccountsTabProps {
  accounts: Destination[];
  workspaceId: string;
}

export function AccountsTab({ accounts, workspaceId }: AccountsTabProps) {
  const router = useRouter();
  const [loadingAction, setLoadingAction] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [removingDialogOpen, setRemovingDialogOpen] = useState<string | null>(null);
  /** The past-posts dialog, open while there is one, and the connect it is for. */
  const [choice, setChoice] = useState<(ConnectChoiceState & { target: ConnectTarget }) | null>(
    null,
  );
  /** The button that asked for the dialog: focus goes back to it on close. */
  const opener = useRef<HTMLElement | null>(null);

  /**
   * Start an Instagram Login grant: preview, then ask or start. Busy state
   * rides the same `loadingAction` tag the other row controls use. The page
   * leaves on success, so the tag is deliberately not cleared there — a
   * re-enabled button on a page that is navigating away invites a second
   * click that retires the first state.
   */
  async function startGrant(tag: string, target: ConnectTarget, button: HTMLElement) {
    opener.current = button;
    setError(null);
    setLoadingAction(tag);
    const step = await beginConnect(target);
    if (step.kind === "redirect") {
      window.location.assign(step.url);
      return;
    }
    setLoadingAction(null);
    if (step.kind === "refused") {
      setError(connectRefusalCopy(step.error, step.status));
      return;
    }
    setChoice({
      target,
      preview: step.preview,
      useHistory: step.preview.defaultOn,
      notice: null,
      error: null,
      busy: false,
    });
  }

  /** The dialog's Continue: the choice goes with the start, or the dialog asks again. */
  async function confirmChoice() {
    if (!choice || choice.busy) return;
    const asked = { ...choice, notice: null, error: null };
    setChoice({ ...asked, busy: true });
    const step = await confirmConnect(asked.target, asked.preview, asked.useHistory);
    if (step.kind === "redirect") {
      window.location.assign(step.url);
      return;
    }
    if (step.kind === "ask") {
      // A fresh preview after a conflict: the person's choice stands, the
      // shares it would leave are the new ones, and they are asked again.
      setChoice({ ...asked, preview: step.preview, notice: step.notice });
      return;
    }
    if (step.error === "archive_not_activated") {
      setChoice(null);
      setError(connectRefusalCopy(step.error, step.status));
      return;
    }
    setChoice({ ...asked, error: connectRefusalCopy(step.error, step.status) });
  }

  /** ADD a destination: no account named — Instagram says which signed in. */
  function connectNewAccount(button: HTMLElement) {
    return startGrant("connect-new", { workspaceId, accountId: null }, button);
  }

  /** Connect or reconnect ONE existing destination (#1220). */
  function connectDestination(accountId: string, button: HTMLElement) {
    return startGrant(`connect-${accountId}`, { workspaceId, accountId }, button);
  }

  /**
   * Remove = the port's `disable_account`. The row
   * leaves the list; connecting the same account again brings it back.
   */
  async function removeAccount(accountId: string) {
    setError(null);
    setLoadingAction(`remove-${accountId}`);
    try {
      const result = await submitDisableAccount(workspaceId, accountId);
      if (!result.ok) {
        // The banner lives outside the dialog; close the dialog first so the
        // refusal is what the person sees, not a spinner that stopped.
        setRemovingDialogOpen(null);
        setError(disableAccountRefusalCopy(result.error, result.status));
        return;
      }
      setRemovingDialogOpen(null);
      router.refresh();
    } finally {
      setLoadingAction(null);
    }
  }

  return (
    <div className="space-y-6 pt-4">
      {error && (
        <Notice tone="error" className="mb-4" onDismiss={() => setError(null)}>
          {error}
        </Notice>
      )}
      <Card>
        <CardHeader className="flex flex-row items-center justify-between gap-4">
          <CardTitle>Instagram accounts</CardTitle>
          {/*
            A destination is ADDED by connecting.
            Nothing is typed: Instagram says which account signed in, and the
            callback lands it on the row it already has here or on a new,
            scheduled one — one source of truth for the handle.
          */}
          <Button
            type="button"
            size="sm"
            onClick={(event) => connectNewAccount(event.currentTarget)}
            disabled={loadingAction !== null}
          >
            {loadingAction === "connect-new" ? "Opening Instagram..." : "Connect Instagram"}
          </Button>
        </CardHeader>
        <CardContent>
          {accounts.length === 0 ? (
            <p className="text-sm text-muted-foreground py-4 text-center">
              No Instagram account connected yet. Use Connect Instagram to add the
              account this workspace posts to; what it produces is posts waiting for
              your approval.
            </p>
          ) : (
            <div className="space-y-3">
              {accounts.map((account) => {
                const handleText = destinationHandle(account.handle);
                const stateBadge = destinationStateBadge(account.state);
                const connectControl = connectControlFor(account.credential_status);
                return (
                <div
                  key={account.id}
                  className="flex items-center justify-between gap-4 rounded-lg border p-4"
                >
                  <div className="min-w-0 flex-1">
                    <div className="flex items-center gap-2">
                      <p className="font-medium truncate">
                        {destinationName(account)}
                      </p>
                      {/* Unconditional. A row that renders at all states what
                          it is: a non-active value with NO badge would look
                          like a not-yet-loaded row or a thrown component
                          (#1121). */}
                      <Badge
                        variant="secondary"
                        className={TONE_CLASS[stateBadge.tone]}
                      >
                        {stateBadge.label}
                      </Badge>
                    </div>
                    {handleText && (
                      <p className="text-sm text-muted-foreground">@{handleText}</p>
                    )}
                    {/* The credential is a separate fact from the schedule
                        state above it: a destination can be scheduled and
                        never connected, which is every manual-mode row. */}
                    <p className="text-xs text-muted-foreground">
                      {destinationConnectionCaption(account.credential_status)}
                    </p>
                  </div>
                  <div className="flex items-center gap-2">
                    {connectControl && (
                      <Button
                        variant={connectControl.kind === "reconnect" ? "default" : "outline"}
                        size="sm"
                        onClick={(event) => connectDestination(account.id, event.currentTarget)}
                        disabled={loadingAction !== null}
                      >
                        {loadingAction === `connect-${account.id}`
                          ? "Opening Instagram..."
                          : connectControl.label}
                        {/* The row's handle, for a list of identical buttons. */}
                        {handleText && <span className="sr-only"> @{handleText}</span>}
                      </Button>
                    )}
                      <Dialog open={removingDialogOpen === account.id} onOpenChange={(open) => setRemovingDialogOpen(open ? account.id : null)}>
                        <DialogTrigger asChild>
                          <Button
                            variant="destructive"
                            size="sm"
                            disabled={loadingAction !== null}
                          >
                            Remove
                          </Button>
                        </DialogTrigger>
                        <DialogContent>
                          <DialogHeader>
                            <DialogTitle>Remove Account</DialogTitle>
                            <DialogDescription>
                              Remove{" "}
                              {handleText ? `@${handleText}` : "this destination"} from this
                              workspace? Its schedule stops, its Instagram connection is
                              revoked, and posts waiting for approval are cancelled.
                              Connecting the account again brings it back.
                            </DialogDescription>
                          </DialogHeader>
                          <DialogFooter>
                            <DialogClose asChild>
                              <Button variant="outline">Cancel</Button>
                            </DialogClose>
                            <Button
                              variant="destructive"
                              onClick={() => removeAccount(account.id)}
                              disabled={loadingAction === `remove-${account.id}`}
                            >
                              {loadingAction === `remove-${account.id}`
                                ? "Removing..."
                                : "Remove"}
                            </Button>
                          </DialogFooter>
                        </DialogContent>
                      </Dialog>
                  </div>
                </div>
                );
              })}
            </div>
          )}

        </CardContent>
      </Card>
      <ConnectChoiceDialog
        state={choice}
        returnFocusTo={opener}
        on={{
          toggle: (on) => setChoice((open) => (open && !open.busy ? { ...open, useHistory: on } : open)),
          confirm: confirmChoice,
          cancel: () => setChoice(null),
        }}
      />
    </div>
  );
}
