"use client";

import { useEffect, useId, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Notice } from "@/components/ui/notice";
import {
  offboardingRefusalCopy,
  submitOffboardWorkspace,
  submitRestoreWorkspace,
} from "@/lib/command-client";

/**
 * Delete this workspace, and the way back (#1127, `06` §1).
 *
 * `offboard_workspace` and `restore_workspace` were built and owner-gated at
 * the port before any surface offered them; this card is that surface. It is
 * rendered for owners only (`settings/page.tsx` decides from the session's
 * membership role), and the port refuses everyone else regardless.
 *
 * What "delete" does, in the order the executor runs it (`offboarding.py`):
 * live posting work drains, then Instagram and Google Drive access is REVOKED
 * at the providers, then the workspace sits invisible to the scheduler for a
 * 30-day grace window, then it is deleted with everything in it. Restoring
 * inside the window brings the workspace back but cannot un-revoke: accounts
 * whose access was revoked come back needing reconnection before posting
 * resumes (`restore_workspace`, #1185). The copy below says exactly that,
 * because a restore that reads as "undo" would manufacture a belief.
 *
 * The confirmation is the workspace's name, typed exactly. Case-sensitive,
 * inner whitespace significant, outer whitespace forgiven — see
 * `deletionConfirmed`.
 */

export function deletionConfirmed(typed: string, workspaceName: string): boolean {
  const name = workspaceName.trim();
  if (name.length === 0) return false;
  return typed.trim() === name;
}

/** "until 2026-10-02", from the server's deadline; never a date invented here. */
export function restoreDeadlineCopy(restorableUntil: string | null): string {
  if (!restorableUntil) return "until the grace period ends";
  return `until ${restorableUntil.slice(0, 10)}`;
}

export function DangerZoneCard({
  workspaceId,
  workspaceName,
  state,
  restorableUntil,
}: {
  workspaceId: string;
  workspaceName: string;
  state: string;
  restorableUntil: string | null;
}) {
  const router = useRouter();
  const statusId = useId();
  const [open, setOpen] = useState(false);
  const [typed, setTyped] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const buttonRef = useRef<HTMLButtonElement>(null);
  const moveFocus = useRef(false);

  // Deleting and restoring each replace the card's one button with the other,
  // and the button that goes takes focus with it. So focus is moved by hand to
  // the one that takes its place, rather than left on <body>. Only after this
  // card's own action, since any other re-read of the page can change the
  // state too. And only focus that was dropped: a person who has already moved
  // on is left where they are.
  useEffect(() => {
    if (!moveFocus.current) return;
    moveFocus.current = false;
    if (document.activeElement === document.body) buttonRef.current?.focus();
  }, [state]);

  async function offboard() {
    setError(null);
    setBusy(true);
    const result = await submitOffboardWorkspace(workspaceId);
    setBusy(false);
    if (!result.ok) {
      setError(offboardingRefusalCopy(result.error, result.status));
      return;
    }
    setOpen(false);
    setTyped("");
    moveFocus.current = true;
    // Re-read: this card, the header and the switcher all render the state.
    router.refresh();
  }

  async function restore() {
    setError(null);
    setBusy(true);
    const result = await submitRestoreWorkspace(workspaceId);
    setBusy(false);
    if (!result.ok) {
      setError(offboardingRefusalCopy(result.error, result.status));
      return;
    }
    moveFocus.current = true;
    router.refresh();
  }

  if (state === "offboarding") {
    return (
      <Card className="border-red-200">
        <CardHeader>
          <CardTitle>This workspace is being deleted</CardTitle>
        </CardHeader>
        <CardContent className="space-y-4">
          <p id={statusId} className="text-sm text-muted-foreground">
            Posting has stopped and connected Instagram and Google Drive access
            has been revoked. Everything in the workspace is deleted when the
            grace period ends. You can restore it {restoreDeadlineCopy(restorableUntil)}.
            Restoring brings the workspace back; accounts whose access was revoked
            will need reconnecting before posting resumes.
          </p>
          {error && (
            <Notice tone="error">{error}</Notice>
          )}
          <Button
            ref={buttonRef}
            onClick={restore}
            disabled={busy}
            aria-describedby={statusId}
          >
            {busy ? "Restoring..." : "Restore workspace"}
          </Button>
        </CardContent>
      </Card>
    );
  }

  return (
    <Card className="border-red-200">
      <CardHeader>
        <CardTitle>Delete workspace</CardTitle>
      </CardHeader>
      <CardContent className="space-y-4">
        <p className="text-sm text-muted-foreground">
          Deleting stops posting, revokes the workspace&apos;s Instagram and Google
          Drive access immediately, and removes the workspace with its accounts,
          sources, media index and history after a 30-day grace period. During
          those 30 days you can restore it from here.
        </p>
        {error && !open && (
          <Notice tone="error">{error}</Notice>
        )}
        <Dialog
          open={open}
          onOpenChange={(next) => {
            if (busy) return;
            if (next) setError(null);
            setOpen(next);
          }}
        >
          <DialogTrigger asChild>
            <Button ref={buttonRef} variant="destructive">
              Delete this workspace...
            </Button>
          </DialogTrigger>
          <DialogContent>
            <DialogHeader>
              <DialogTitle>Delete {workspaceName}?</DialogTitle>
              <DialogDescription>
                Type the workspace name exactly to confirm. Access to connected
                accounts is revoked as soon as you confirm; the workspace itself
                is deleted after the 30-day grace period unless you restore it.
              </DialogDescription>
            </DialogHeader>
            <div className="space-y-2">
              <Label htmlFor="confirm-workspace-name">Workspace name</Label>
              <Input
                id="confirm-workspace-name"
                value={typed}
                onChange={(e) => setTyped(e.target.value)}
                placeholder={workspaceName}
                autoComplete="off"
              />
            </div>
            {error && (
              <Notice tone="error">{error}</Notice>
            )}
            <DialogFooter>
              <Button variant="outline" onClick={() => setOpen(false)} disabled={busy}>
                Cancel
              </Button>
              <Button
                variant="destructive"
                onClick={offboard}
                disabled={busy || !deletionConfirmed(typed, workspaceName)}
              >
                {busy ? "Deleting..." : "Delete workspace"}
              </Button>
            </DialogFooter>
          </DialogContent>
        </Dialog>
      </CardContent>
    </Card>
  );
}
