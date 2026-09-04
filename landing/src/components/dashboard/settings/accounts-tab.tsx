"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
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
  addDestination,
  addDestinationRefusalCopy,
  connectControlFor,
  destinationConnectRefusalCopy,
  destinationConnectionCaption,
  destinationHandle,
  destinationIsActive,
  destinationStateBadge,
  requestDestinationConnect,
} from "@/lib/destination";
import type { DestinationStateBadge } from "@/lib/destination";
import type { Destination } from "@/lib/types";

/**
 * `switch-account` and `remove-account` are real controls whose routes are not
 * wired yet (#1063 / epic P6, F5 locked (b)) — DISABLED WITH A REASON, not
 * removed. The distinction is deliberate: a control that was always a lie gets
 * deleted (the Connect button below, whose `oauth-url` route does not exist),
 * while a control that is real and is coming back stays visible and inert, so
 * the screen does not lose a capability the user is about to get.
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
  const [handle, setHandle] = useState("");
  const [adding, setAdding] = useState(false);
  // Field-level, NOT the shared banner at the top of the tab. That banner is
  // for switch/remove, which act on a row far from it; a refusal about what was
  // typed belongs beside the field it is about, and is what `aria-describedby`
  // can point at. Success and failure therefore land in the same place.
  const [addOutcome, setAddOutcome] = useState<
    { ok: true; text: string } | { ok: false; text: string } | null
  >(null);

  /**
   * Add the destination. NOT gated on `editable`: that flag marks controls
   * disabled because their route does not exist yet, and this one's does
   * (#1089). Wiring a working control behind it would hide the thing this
   * change exists to deliver.
   */
  async function submitHandle(event: React.FormEvent) {
    event.preventDefault();
    const typed = handle.trim();
    if (!typed || adding) return;
    setAddOutcome(null);
    setAdding(true);
    try {
      const result = await addDestination(workspaceId, typed);
      if (!result.ok) {
        setAddOutcome({ ok: false, text: addDestinationRefusalCopy(result.error) });
        return;
      }
      setHandle("");
      // "Added" and "you already had that one" are different sentences and the
      // route carries which happened, so say the true one rather than a generic
      // success that makes a duplicate submit look like a second destination.
      setAddOutcome({
        ok: true,
        text: result.created
          ? `Added @${typed}. It is now scheduled.`
          : `@${typed} was already a destination here — nothing changed.`,
      });
      router.refresh();
    } finally {
      // In a `finally` like the two handlers below it. `addDestination` returns
      // a union rather than throwing, so this cannot fire today — but a form
      // stuck disabled is the failure that leaves no way out of the screen.
      setAdding(false);
    }
  }

  /**
   * Start the Instagram Login grant for ONE destination (#1220 step 2). Busy
   * state rides the same `loadingAction` tag the other row controls use. The
   * page leaves on success, so the tag is deliberately not cleared there — a
   * re-enabled button on a page that is navigating away invites a second
   * click that retires the first state.
   */
  async function connectDestination(accountId: string) {
    setError(null);
    setLoadingAction(`connect-${accountId}`);
    const result = await requestDestinationConnect(workspaceId, accountId);
    if (!result.ok) {
      setLoadingAction(null);
      setError(destinationConnectRefusalCopy(result.error));
      return;
    }
    window.location.assign(result.authorizationUrl);
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
        <CardHeader>
          <CardTitle className="text-base">Instagram Accounts</CardTitle>
        </CardHeader>
        <CardContent>
          {accounts.length === 0 ? (
            <p className="text-sm text-muted-foreground py-4 text-center">
              No Instagram destination yet. Add the handle you post to below.
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

          {/*
            The OAuth Connect button is still GONE, not gated: `openOAuthWindow`
            called `oauth-url/instagram`, which is not a route on this API
            (#1063), and it was per-workspace against a per-source flow.

            What replaces it is NOT that button. This form adds a DESTINATION —
            the handle this workspace posts to — which needs no credential and
            no Meta call, because a workspace with `api_publishing_enabled`
            false publishes through a person. The OAuth leg is milestone 2 and
            arrives beside this, not instead of it (#1089).
          */}
          <form onSubmit={submitHandle} className="mt-6 border-t pt-4">
            <Label htmlFor="destination-handle">
              Add the Instagram handle you post to
            </Label>
            <p className="mt-1 text-xs text-muted-foreground">
              Adding a handle needs no Instagram login: it starts the schedule,
              and what that produces is posts waiting for your approval. To let
              Storydump publish for you, use Connect Instagram on the account
              once it is listed above.
            </p>
            <div className="mt-3 flex items-center gap-2">
              <span
                aria-hidden="true"
                className="text-sm text-muted-foreground"
              >
                @
              </span>
              <Input
                id="destination-handle"
                name="handle"
                value={handle}
                onChange={(e) => {
                  setHandle(e.target.value);
                  // Clear on the first keystroke. A green line lingering over a
                  // freshly typed handle reads as if THAT one was added.
                  if (addOutcome) setAddOutcome(null);
                }}
                disabled={adding}
                autoComplete="off"
                placeholder="yourhandle"
                aria-describedby={
                  addOutcome ? "destination-handle-outcome" : "destination-handle-hint"
                }
                className="flex-1"
              />
              <Button type="submit" size="sm" disabled={adding || !handle.trim()}>
                {adding ? "Adding..." : "Add"}
              </Button>
            </div>
            <p id="destination-handle-hint" className="sr-only">
              Just the username, without the at sign.
            </p>
            {addOutcome && (
              <p
                id="destination-handle-outcome"
                role="status"
                className={`mt-3 text-sm ${
                  addOutcome.ok ? "text-green-700" : "text-red-700"
                }`}
              >
                {addOutcome.text}
              </p>
            )}
          </form>
        </CardContent>
      </Card>
    </div>
  );
}
