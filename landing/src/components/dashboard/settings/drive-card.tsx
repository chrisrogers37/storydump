"use client";

import { useId, useState } from "react";
import { useRouter } from "next/navigation";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { TONE_CLASS } from "@/components/dashboard/tone";
import type { DriveStatus, SourceRow } from "@/lib/dashboard-payloads";
import {
  connectedFolderRefs,
  driveConnectControl,
  driveConnectRefusalCopy,
  driveConnectWarning,
  driveConnectedSince,
  driveStatusBadge,
  folderRemoveOutcome,
  listedDriveSources,
  removeDriveFolder,
  removeFolderWarning,
  requestDriveConnect,
  sourceFolderName,
  sourceStateLabel,
} from "@/lib/drive";
import type { SourceMix } from "@/lib/source-mix";
import { settingsRefusalCopy, submitCommand } from "@/lib/command-client";
import { DriveFolderPickerDialog } from "./drive-folder-picker";

/**
 * ── Google Drive is connected ONCE, per workspace ──────────────────────────
 *
 * #1165: one Google grant per workspace, every folder under
 * it. The card shows the grant (from `GET /workspaces/{ws}/drive`, the
 * `drive` prop) and the folders picked under it (the `gdrive` sources); "Add
 * folder" opens a browser of the connected Drive, read through the grant, and
 * a pick creates the source armed for its first sync. Removing a folder pauses
 * it (nothing is deleted); disconnecting Drive revokes the one grant and
 * pauses every folder.
 */
export function DriveCard({
  sources,
  drive,
  mix,
  workspaceId,
  onError,
  onNotice,
}: {
  /** The workspace's sources, unflattened — this card renders them per row. */
  sources: SourceRow[];
  /** The workspace's Google Drive grant; null = could not be loaded. */
  drive: DriveStatus | null;
  /**
   * The workspace's posting mix as the page read it, when it is converted
   * (protocol 2); null on protocol 1. A converted workspace's mix moves with
   * its sources: picking or removing a folder is made against its revision and
   * resets the shares (`lib/drive.ts`).
   */
  mix: SourceMix | null;
  workspaceId: string;
  /** The tab owns the banner both cards speak through. */
  onError: (message: string | null) => void;
  onNotice: (message: string | null) => void;
}) {
  const router = useRouter();
  const [syncingId, setSyncingId] = useState<string | null>(null);
  const [disconnecting, setDisconnecting] = useState(false);
  const [connecting, setConnecting] = useState(false);
  const [removingId, setRemovingId] = useState<string | null>(null);
  // The folder whose removal is waiting on a confirmation: only where removing
  // it resets the shares (`removeFolderWarning`).
  const [confirmingId, setConfirmingId] = useState<string | null>(null);
  const hintId = useId();
  const confirmId = useId();

  // Only removal takes a folder off the list. A disconnect pauses it while
  // keeping its synced media and approval position.
  const driveSources = listedDriveSources(sources);
  const grant = driveStatusBadge(drive?.status);
  const connectControl = drive ? driveConnectControl(drive.status) : null;
  const connectWarning = drive ? driveConnectWarning(drive.status) : null;
  const driveActive = drive?.status === "active";

  /** Folders that are sources here already: the picker greys them. */
  const connectedRefs = connectedFolderRefs(sources);

  /** What removing this folder does to the shares, said first; null where it changes none. */
  const removalWarning = (source: SourceRow) =>
    removeFolderWarning(mix, source.id, sourceFolderName(source));
  /** Whether its removal is waiting on a Confirm. */
  const isConfirming = (source: SourceRow) =>
    confirmingId === source.id && removalWarning(source) !== null;

  /**
   * Start the WORKSPACE's grant, then hand the browser to Google.
   *
   * `window.location.assign`, not a new tab: the callback is a real
   * navigation (`GET /auth/google-drive/callback`) that lands back in this
   * app, so a same-tab redirect needs no listener and no guessing about when
   * the person came back. No `setConnecting(false)` on success: the page is
   * leaving, and clearing it would flash the button back to its resting
   * label during the navigation.
   */
  async function connectDrive() {
    onError(null);
    onNotice(null);
    setConnecting(true);
    const result = await requestDriveConnect(workspaceId);
    if (!result.ok) {
      setConnecting(false);
      onError(driveConnectRefusalCopy(result.error));
      return;
    }
    window.location.assign(result.authorizationUrl);
  }

  /**
   * Remove a folder from syncing — a pause on the API side, never a delete.
   * On a converted workspace it is made against the mix's revision, and a
   * refusal that says the page's mix is out of date reads it again.
   */
  async function removeFolder(sourceId: string) {
    onError(null);
    onNotice(null);
    setConfirmingId(null);
    setRemovingId(sourceId);
    const result = await removeDriveFolder(workspaceId, sourceId, mix?.revision ?? null);
    setRemovingId(null);
    const outcome = folderRemoveOutcome(result);
    if (outcome.kind === "stay") {
      onError(outcome.error);
      if (outcome.reload) router.refresh();
      return;
    }
    onNotice(outcome.message);
    router.refresh();
  }

  /**
   * Remove is one press unless it resets the shares: a folder that is on in a
   * converted workspace holds one, so removing it says so first and waits for
   * a Confirm (`removeFolderWarning`).
   */
  function askRemove(source: SourceRow) {
    if (removalWarning(source) === null) {
      void removeFolder(source.id);
      return;
    }
    onError(null);
    onNotice(null);
    setConfirmingId(source.id);
  }

  /**
   * Disconnect Google Drive — REVOKE AND PAUSE, never a delete.
   *
   * The executor revokes the workspace's one grant, KEEPS every row, and sets
   * each folder `paused` rather than `error`: a disconnect is a decision, not
   * a fault, and `error` is reserved for faults so the stranded-source alert
   * stays meaningful. The copy says "asked Google to revoke", never
   * "revoked": the local revocation is immediate and certain, while the
   * Google-side call is a BEST-EFFORT background job so a provider outage
   * cannot block the person's disconnect.
   */
  async function disconnectDrive() {
    onError(null);
    onNotice(null);
    setDisconnecting(true);
    const result = await submitCommand(workspaceId, "disconnect_account", {});
    setDisconnecting(false);

    if (!result.ok) {
      onError(settingsRefusalCopy(result.error, result.status));
      return;
    }
    onNotice(
      "Disconnected. Every folder is paused and access is revoked here — we have also asked Google to revoke it on their side. Your folders and everything already synced stay where they are.",
    );
    router.refresh();
  }

  /**
   * Sync ONE source, now.
   *
   * This is the `sync_now` command. It is
   * per-SOURCE — the executor reads `source_id` and refuses without it — so
   * the button carries its row's id, the same shape as Connect.
   *
   * The port has THREE answers and only one of them means a sync started:
   * `enqueued` with a job id, or `executed` carrying `sync: "already_pending"`
   * when one is already queued for that source (`unless_pending`). Both are
   * 2xx. Reporting the second as a fresh sync would tell someone their click
   * did something it did not, so the two are said differently.
   */
  async function syncSource(sourceId: string) {
    onError(null);
    onNotice(null);
    setSyncingId(sourceId);
    const result = await submitCommand(workspaceId, "sync_now", {
      source_id: sourceId,
    });
    setSyncingId(null);

    if (!result.ok) {
      onError(settingsRefusalCopy(result.error, result.status));
      return;
    }
    onNotice(
      result.data?.sync === "already_pending"
        ? "A sync is already queued for that folder."
        : "Sync started.",
    );
    router.refresh();
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle>Google Drive</CardTitle>
      </CardHeader>
      <CardContent className="space-y-4">
        <div className="space-y-2">
          {drive === null ? (
            <p className="text-sm text-muted-foreground">
              The Google Drive connection could not be loaded just now. Reload to
              try again.
            </p>
          ) : (
            <div className="flex flex-wrap items-center justify-between gap-3">
              <div className="flex flex-wrap items-center gap-2">
                {/* GREEN BELONGS TO THE GRANT ALONE: `driveStatusBadge` is
                    pinned so only `active` ever carries this tone. The map is
                    `components/dashboard/tone.ts`. */}
                <Badge variant="secondary" className={TONE_CLASS[grant.tone]}>
                  {grant.label}
                </Badge>
                <p className="text-sm text-muted-foreground">
                  {driveActive
                    ? `Connected${driveConnectedSince(drive) ? ` since ${driveConnectedSince(drive)}` : ""}. The folders below sync from this Google account.`
                    : drive.status === "none"
                      ? "Connect the Google account whose Drive holds your media, then pick the folders to sync."
                      : "Google no longer accepts this workspace's access. Reconnect to resume syncing."}
                </p>
              </div>
              <div className="flex items-center gap-2">
                {connectControl && (
                  <Button
                    size="sm"
                    variant={driveActive ? "outline" : "default"}
                    onClick={connectDrive}
                    disabled={connecting}
                    aria-describedby={connectWarning ? hintId : undefined}
                  >
                    {connecting ? "Opening Google..." : connectControl.label}
                  </Button>
                )}
                {drive.status !== "none" && (
                  <Button
                    variant="destructive"
                    size="sm"
                    onClick={disconnectDrive}
                    disabled={disconnecting}
                  >
                    {disconnecting ? "Disconnecting..." : "Disconnect"}
                  </Button>
                )}
              </div>
            </div>
          )}
          {connectWarning && (
            <p id={hintId} className="text-xs text-muted-foreground">
              {connectWarning}
            </p>
          )}
        </div>

        <div className="space-y-2 border-t pt-4">
          <div className="flex items-center justify-between gap-3">
            <p className="text-sm font-medium">Folders</p>
            <DriveFolderPickerDialog
              workspaceId={workspaceId}
              connectedRefs={connectedRefs}
              disabled={!driveActive}
              mix={mix}
              onOpen={() => {
                onError(null);
                onNotice(null);
              }}
              onPicked={(message) => {
                onNotice(message);
                router.refresh();
              }}
              onStale={() => router.refresh()}
            />
          </div>
          {driveSources.length === 0 ? (
            <p className="text-sm text-muted-foreground">
              {driveActive
                ? "No folder is picked yet. Add one to start syncing."
                : "Connect Google Drive to pick the folders to sync."}
            </p>
          ) : (
            <ul className="divide-y">
              {driveSources.map((source) => (
                <li
                  key={source.id}
                  className="flex flex-wrap items-center justify-between gap-3 py-3"
                >
                  <div className="space-y-0.5">
                    <div className="flex items-center gap-2">
                      <p className="font-medium">
                        {sourceFolderName(source)}
                      </p>
                      {/* The source's own operating state, coloured only
                          when it is a problem: healthy is unremarkable. */}
                      {source.state !== "active" && (
                        <Badge
                          variant="secondary"
                          className="bg-amber-100 text-amber-900"
                        >
                          {sourceStateLabel(source.state)}
                        </Badge>
                      )}
                    </div>
                    <p className="text-sm text-muted-foreground">
                      {source.last_sync_success_at
                        ? `Last synced ${new Date(source.last_sync_success_at).toLocaleString()}`
                        : "No sync has completed yet"}
                      {source.folder_name === null && source.folder_ref
                        ? ` · folder ${source.folder_ref}`
                        : ""}
                    </p>
                  </div>
                  <div className="flex items-center gap-2">
                    <Button
                      variant="outline"
                      size="sm"
                      onClick={() => syncSource(source.id)}
                      disabled={syncingId !== null || !driveActive}
                    >
                      {syncingId === source.id ? "Syncing..." : "Sync Now"}
                    </Button>
                    <Button
                      variant="outline"
                      size="sm"
                      onClick={() => askRemove(source)}
                      disabled={removingId !== null || isConfirming(source)}
                    >
                      {removingId === source.id ? "Removing..." : "Remove"}
                    </Button>
                  </div>
                  {isConfirming(source) && (
                    <div
                      role="group"
                      aria-labelledby={`${confirmId}-${source.id}`}
                      className="basis-full space-y-2 rounded-md border p-3"
                    >
                      <p id={`${confirmId}-${source.id}`} className="break-words text-sm">
                        {removalWarning(source)}
                      </p>
                      <div className="flex flex-wrap gap-2">
                        <Button
                          size="sm"
                          onClick={() => removeFolder(source.id)}
                          disabled={removingId !== null}
                        >
                          Confirm remove
                        </Button>
                        <Button
                          size="sm"
                          variant="outline"
                          onClick={() => setConfirmingId(null)}
                          disabled={removingId !== null}
                        >
                          Cancel
                        </Button>
                      </div>
                    </div>
                  )}
                </li>
              ))}
            </ul>
          )}
          <p className="text-xs text-muted-foreground">
            Removing a folder stops syncing it and takes its media out of the
            library; its posting history stays, and the media comes back if you
            connect the folder again — or if another connected folder holds the
            same files. Disconnecting Google Drive pauses every folder and
            revokes access here, and asks Google to revoke it on their side.
          </p>
        </div>
      </CardContent>
    </Card>
  );
}
