"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { TONE_CLASS } from "@/components/dashboard/tone";
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
  connectControlFor,
  destinationConnectRefusalCopy,
  destinationConnectionCaption,
  destinationHandle,
  destinationStateBadge,
  requestDestinationConnect,
  requestWorkspaceConnect,
} from "@/lib/destination";
import type { DestinationConnectResult } from "@/lib/destination";
import type { Destination } from "@/lib/types";

/**
 * Connect is real and ungated: the header's *Connect Instagram* ADDS a
 * destination through the Instagram Login grant (owner ruling 2026-09-04),
 * each row's Connect/Reconnect acts on the account it names, and Remove is
 * the port's `disable_account`.
 *
 * SWITCHING IS GONE FROM THIS SCREEN (TD-D3). It was a "Make Active" button
 * disabled with a reason, on the argument that the screen should not lose a
 * capability it was about to get. The capability did not arrive: the button
 * POSTed to `/api/dashboard/switch-account`, a BFF proxy onto a target path
 * that does not exist and has no entry in `COMMAND_SPECS`. A control that
 * cannot be pressed is not a promise, it is furniture, and the proxy behind
 * it was an authenticated door onto 22 routes the API stopped serving. When
 * P6 lands, switching comes back as a `switch_account` row in
 * `lib/commands.ts` and a button that calls `submitCommand`, like every
 * other write on this tab.
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

  /**
   * Start an Instagram Login grant. Busy state rides the same `loadingAction`
   * tag the other row controls use. The page leaves on success, so the tag is
   * deliberately not cleared there — a re-enabled button on a page that is
   * navigating away invites a second click that retires the first state.
   */
  async function startGrant(tag: string, request: () => Promise<DestinationConnectResult>) {
    setError(null);
    setLoadingAction(tag);
    const result = await request();
    if (!result.ok) {
      setLoadingAction(null);
      setError(destinationConnectRefusalCopy(result.error));
      return;
    }
    window.location.assign(result.authorizationUrl);
  }

  /** ADD a destination: no account named — Instagram says which signed in. */
  function connectNewAccount() {
    return startGrant("connect-new", () => requestWorkspaceConnect(workspaceId));
  }

  /** Connect or reconnect ONE existing destination (#1220 step 2). */
  function connectDestination(accountId: string) {
    return startGrant(`connect-${accountId}`, () =>
      requestDestinationConnect(workspaceId, accountId),
    );
  }

  /**
   * Remove = the port's `disable_account` (owner decision 2026-09-04). The row
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
        <div className="mb-4 rounded-md border border-red-200 bg-red-50 p-3 text-sm text-red-800 flex items-center justify-between">
          <span>{error}</span>
          <button onClick={() => setError(null)} className="ml-2 text-red-600 hover:text-red-800 font-medium">Dismiss</button>
        </div>
      )}
      <Card>
        <CardHeader className="flex flex-row items-center justify-between gap-4">
          <CardTitle className="text-base">Instagram Accounts</CardTitle>
          {/*
            A destination is ADDED by connecting (owner ruling 2026-09-04).
            Nothing is typed: Instagram says which account signed in, and the
            callback lands it on the row it already has here or on a new,
            scheduled one — one source of truth for the handle.
          */}
          <Button
            type="button"
            size="sm"
            onClick={connectNewAccount}
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
                        {account.display_name ?? handleText ?? "Unnamed destination"}
                      </p>
                      {/* Unconditional. A row that renders at all states what
                          it is: the three non-active values used to render as
                          NO badge, which is what a not-yet-loaded row and a
                          thrown component also look like (#1121). */}
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
                        onClick={() => connectDestination(account.id)}
                        disabled={loadingAction !== null}
                      >
                        {loadingAction === `connect-${account.id}`
                          ? "Opening Instagram..."
                          : connectControl.label}
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
    </div>
  );
}
