"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
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
import { postApi } from "@/lib/dashboard-api";
import {
  connectControlFor,
  destinationConnectRefusalCopy,
  destinationConnectionCaption,
  destinationHandle,
  destinationIsActive,
  destinationStateBadge,
  requestDestinationConnect,
  requestWorkspaceConnect,
} from "@/lib/destination";
import type { DestinationConnectResult, DestinationStateBadge } from "@/lib/destination";
import type { Destination } from "@/lib/types";

/**
 * Connect is real and ungated: the header's *Connect Instagram* ADDS a
 * destination through the Instagram Login grant (owner ruling 2026-09-04), and
 * each row's Connect/Reconnect acts on the account it names. `switch-account`
 * and `remove-account` are real controls whose routes are not wired yet
 * (#1063 / epic P6) — DISABLED WITH A REASON, not removed, so the screen does
 * not lose a capability the user is about to get.
 */
const DISABLED_REASON =
  "Not wired up yet — changing accounts is not available on this API version.";

/**
 * Tone to Tailwind. Semantics come from `destinationStateBadge`; the classes
 * live here because that is where the rest of this screen's visual language
 * does. `inert` is deliberately the muted pair rather than a third colour —
 * `disabled` and `moved` differ in their LABEL, and inventing a colour per
 * state would say they differ in kind when they do not.
 *
 * Keyed on the tone UNION, so adding a tone without a class is a compile
 * error rather than a row that renders an unstyled badge.
 */
const STATE_TONE_CLASS: Record<DestinationStateBadge["tone"], string> = {
  active: "bg-green-100 text-green-800",
  attention: "bg-amber-100 text-amber-900",
  inert: "bg-muted text-muted-foreground",
};

interface AccountsTabProps {
  /** False while the write routes do not exist (#1063). */
  editable: boolean;
  accounts: Destination[];
  workspaceId: string;
}

export function AccountsTab({ accounts, editable, workspaceId }: AccountsTabProps) {
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

  async function switchAccount(accountId: string) {
    setError(null);
    setLoadingAction(`switch-${accountId}`);
    try {
      await postApi("switch-account", { account_id: accountId });
      router.refresh();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to switch account");
    } finally {
      setLoadingAction(null);
    }
  }

  async function removeAccount(accountId: string) {
    setError(null);
    setLoadingAction(`remove-${accountId}`);
    try {
      await postApi("remove-account", { account_id: accountId });
      setRemovingDialogOpen(null);
      router.refresh();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to remove account");
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
                const isActive = destinationIsActive(account.state);
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
                        className={STATE_TONE_CLASS[stateBadge.tone]}
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
                    {!isActive && (
                        <Button
                          variant="outline"
                          size="sm"
                          onClick={() => switchAccount(account.id)}
                          disabled={!editable || loadingAction === `switch-${account.id}`}
                          title={editable ? undefined : DISABLED_REASON}
                        >
                          {loadingAction === `switch-${account.id}`
                            ? "Activating..."
                            : "Make Active"}
                        </Button>
                      )}
                      <Dialog open={removingDialogOpen === account.id} onOpenChange={(open) => setRemovingDialogOpen(open ? account.id : null)}>
                        <DialogTrigger asChild>
                          <Button
                            variant="destructive"
                            size="sm"
                            disabled={!editable}
                            title={editable ? undefined : DISABLED_REASON}
                          >
                            Remove
                          </Button>
                        </DialogTrigger>
                        <DialogContent>
                          <DialogHeader>
                            <DialogTitle>Remove Account</DialogTitle>
                            <DialogDescription>
                              Remove{" "}
                              {handleText ? `@${handleText}` : "this destination"}? This
                              will disconnect the account and stop all scheduled
                              posts.
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

          {!editable && (
            <p className="mt-4 text-xs text-muted-foreground">
              Switching and removing accounts is not wired up yet — those
              controls are shown disabled rather than hidden, because they are
              coming back.
            </p>
          )}

        </CardContent>
      </Card>
    </div>
  );
}
