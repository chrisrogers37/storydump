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
import type { InstagramAccount } from "@/lib/types";

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

interface AccountsTabProps {
  /** False while the write routes do not exist (#1063). */
  editable: boolean;
  accounts: InstagramAccount[];
}

export function AccountsTab({ accounts, editable }: AccountsTabProps) {
  const router = useRouter();
  const [loadingAction, setLoadingAction] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [removingDialogOpen, setRemovingDialogOpen] = useState<string | null>(null);
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
              No Instagram accounts connected.
            </p>
          ) : (
            <div className="space-y-3">
              {accounts.map((account) => (
                <div
                  key={account.id}
                  className="flex items-center justify-between gap-4 rounded-lg border p-4"
                >
                  <div className="min-w-0 flex-1">
                    <div className="flex items-center gap-2">
                      <p className="font-medium truncate">
                        {account.display_name}
                      </p>
                      {account.is_active && (
                        <Badge variant="secondary" className="bg-green-100 text-green-800">
                          Active
                        </Badge>
                      )}
                    </div>
                    <p className="text-sm text-muted-foreground">
                      @{account.instagram_username}
                    </p>
                  </div>
                  <div className="flex items-center gap-2">
                    {!account.is_active && (
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
                              Remove @{account.instagram_username}? This will
                              disconnect the account and stop all scheduled posts.
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
              ))}
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
            GONE, not gated. `openOAuthWindow` called `oauth-url/instagram`,
            which is not a route on this API (#1063), and the button was
            per-workspace against a per-source flow — it could not be wired as
            written. It used to sit behind `editable`, which meant P3 flipping
            that flag would have silently restored it (rajan, #1066 review).
            `editable` now gates ONE thing: controls that are disabled because
            they are pending, and that are coming back.
          */}
          <p className="mt-4 text-sm text-muted-foreground">
            Connecting an Instagram account is not available from this screen
            yet.
          </p>
        </CardContent>
      </Card>
    </div>
  );
}
