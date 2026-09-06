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
} from "@/components/ui/dialog";
import type { DriveStatus, SettingsView, SourceRow } from "@/lib/dashboard-payloads";
import {
  addDriveFolder,
  addFolderRefusalCopy,
  driveConnectControl,
  driveConnectRefusalCopy,
  driveConnectedSince,
  driveFoldersRefusalCopy,
  driveStatusBadge,
  fetchDriveFolders,
  removeDriveFolder,
  removeFolderRefusalCopy,
  requestDriveConnect,
  SHARED_ROOT,
} from "@/lib/drive";
import type { DriveFolder } from "@/lib/drive";
import {
  requestTelegramGroupLink,
  requestTelegramLink,
  telegramGroupLinkRefusalCopy,
  telegramLinkRefusalCopy,
} from "@/lib/telegram-link";
import type { ChannelBinding } from "@/lib/types";
import { startCommandFor } from "@/lib/telegram-link";
import { settingsRefusalCopy, submitCommand } from "@/lib/command-client";

/**
 * Integrations, read-only (#1063).
 *
 * Every action on this tab targets a route that does not exist —
 * `disconnect-gdrive`, `sync-media`, and `oauth-url/google-drive` behind the
 * connect button. All three are wired now; nothing on this card is held off.
 *
 * The connection facts are now real: `gdrive_connected` and the source's own
 * `state` come from `GET /workspaces/{ws}/sources`, and the media count from
 * `stats`. What has no source is stated as such rather than defaulted — the
 * previous version rendered `mediaSyncEnabled ?? false` as the flat sentence
 * "Auto-sync disabled", which is a claim about the workspace made from a
 * column that does not exist.
 */
/**
 * ── Google Drive is connected ONCE, per workspace (owner ruling 2026-09-05) ──
 *
 * #1165 lean (b), `07` §15: one Google grant per workspace, every folder under
 * it. The card shows the grant (from `GET /workspaces/{ws}/drive`, the
 * `drive` prop) and the folders picked under it (the `gdrive` sources); "Add
 * folder" opens a browser of the connected Drive, read through the grant, and
 * a pick creates the source armed for its first sync. Removing a folder pauses
 * it (nothing is deleted); disconnecting Drive revokes the one grant and
 * pauses every folder. The paste-a-link form and the per-folder Connect
 * button this replaces are gone: linking a folder before the account was the
 * legacy order, inverted.
 */
export function IntegrationsTab({
  settings,
  sources,
  drive,
  workspaceId,
  telegramLinked,
  bindings = [],
  telegramDisplayName,
}: {
  settings: SettingsView;
  /** The workspace's sources, unflattened — this card renders them per row. */
  sources: SourceRow[];
  /** The workspace's Google Drive grant; null = could not be loaded. */
  drive: DriveStatus | null;
  workspaceId: string;
  /** Whether the signed-in USER has a Telegram identity attached — a fact
   *  about the person, not this workspace (#1172 clause 1). */
  telegramLinked: boolean;
  /** The Telegram chats this WORKSPACE's cards go to (`07` §13); null = could not be loaded. */
  bindings?: ChannelBinding[] | null;
  /** Who that identity is, so a link tapped by the wrong person is visible. */
  telegramDisplayName: string | null;
}) {
  const router = useRouter();
  const [telegramLink, setTelegramLink] = useState<{ link: string; expiresInSeconds: number } | null>(null);
  const [linkingTelegram, setLinkingTelegram] = useState(false);
  const [groupLink, setGroupLink] = useState<{ link: string; expiresInSeconds: number } | null>(null);
  const [mintingGroupLink, setMintingGroupLink] = useState(false);
  const [groupLinkError, setGroupLinkError] = useState<string | null>(null);
  const boundGroups = (bindings ?? []).filter((b) => b.state === "active");

  /** Mint the group-picker link (`07` §13) and SHOW it, like the identity link. */
  async function addTelegramGroup() {
    setGroupLinkError(null);
    setMintingGroupLink(true);
    const result = await requestTelegramGroupLink(workspaceId);
    setMintingGroupLink(false);
    if (!result.ok) {
      setGroupLinkError(telegramGroupLinkRefusalCopy(result.error));
      return;
    }
    setGroupLink({ link: result.link, expiresInSeconds: result.expiresInSeconds });
  }
  const [syncingId, setSyncingId] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [disconnecting, setDisconnecting] = useState(false);
  const [connecting, setConnecting] = useState(false);
  const [removingId, setRemovingId] = useState<string | null>(null);
  const [pickerOpen, setPickerOpen] = useState(false);
  const [pickerStack, setPickerStack] = useState<DriveFolder[]>([]);
  const [pickerFolders, setPickerFolders] = useState<DriveFolder[] | null>(null);
  const [pickerLoading, setPickerLoading] = useState(false);
  const [pickerError, setPickerError] = useState<string | null>(null);
  const [pickingId, setPickingId] = useState<string | null>(null);
  const [pickerRoot, setPickerRoot] = useState<"mine" | "shared">("mine");
  const [pickerTruncated, setPickerTruncated] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // A removed folder is a PAUSED source (nothing is deleted); it leaves the
  // list and comes back when picked again.
  const driveSources = sources.filter((s) => s.provider === "gdrive" && s.state !== "paused");
  const grant = driveStatusBadge(drive?.status);
  const connectControl = drive ? driveConnectControl(drive.status) : null;
  const driveActive = drive?.status === "active";
  const pickerCurrent = pickerStack.length > 0 ? pickerStack[pickerStack.length - 1] : null;

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
    setError(null);
    setNotice(null);
    setConnecting(true);
    const result = await requestDriveConnect(workspaceId);
    if (!result.ok) {
      setConnecting(false);
      setError(driveConnectRefusalCopy(result.error));
      return;
    }
    window.location.assign(result.authorizationUrl);
  }

  /** The folder browser: one listing per level, read through the grant. */
  async function loadFolders(stack: DriveFolder[], root: "mine" | "shared") {
    setPickerLoading(true);
    setPickerError(null);
    setPickerFolders(null);
    setPickerTruncated(false);
    const parent =
      stack.length > 0 ? stack[stack.length - 1].id : root === "shared" ? SHARED_ROOT : null;
    const result = await fetchDriveFolders(workspaceId, parent);
    setPickerLoading(false);
    if (!result.ok) {
      setPickerError(driveFoldersRefusalCopy(result.error));
      return;
    }
    setPickerFolders(result.folders);
    setPickerTruncated(result.truncated);
  }

  function openPicker() {
    setError(null);
    setNotice(null);
    setPickerOpen(true);
    setPickerStack([]);
    setPickerRoot("mine");
    void loadFolders([], "mine");
  }

  function closePicker() {
    setPickerOpen(false);
    setPickerFolders(null);
    setPickerError(null);
    setPickerStack([]);
    setPickerTruncated(false);
  }

  function switchRoot(root: "mine" | "shared") {
    setPickerRoot(root);
    setPickerStack([]);
    void loadFolders([], root);
  }

  function enterFolder(folder: DriveFolder) {
    const next = [...pickerStack, folder];
    setPickerStack(next);
    void loadFolders(next, pickerRoot);
  }

  function goTo(index: number) {
    const next = index < 0 ? [] : pickerStack.slice(0, index + 1);
    setPickerStack(next);
    void loadFolders(next, pickerRoot);
  }

  /**
   * Pick a folder: the source is created (or revived, if it had been removed)
   * and armed for its first sync. `created` matters to a person: the same
   * folder picked twice is the SAME source, and saying "added" both times
   * would hide that.
   */
  async function pickFolder(folder: DriveFolder) {
    setPickerError(null);
    setPickingId(folder.id);
    const result = await addDriveFolder(workspaceId, folder);
    setPickingId(null);
    if (!result.ok) {
      setPickerError(addFolderRefusalCopy(result.error));
      return;
    }
    closePicker();
    setNotice(
      result.created
        ? `"${folder.name}" added. Its first sync starts shortly.`
        : `"${folder.name}" was already a source here — it is syncing again.`,
    );
    router.refresh();
  }

  /** Remove a folder from syncing — a pause on the API side, never a delete. */
  async function removeFolder(sourceId: string) {
    setError(null);
    setNotice(null);
    setRemovingId(sourceId);
    const result = await removeDriveFolder(workspaceId, sourceId);
    setRemovingId(null);
    if (!result.ok) {
      setError(removeFolderRefusalCopy(result.error));
      return;
    }
    setNotice("Folder removed from syncing. What was already synced stays; pick it again to resume.");
    router.refresh();
  }

  /**
   * Disconnect Google Drive — REVOKE AND PAUSE, never a delete (F5 (a)).
   *
   * The executor revokes the workspace's one grant, KEEPS every row, and sets
   * each folder `paused` rather than `error`: a disconnect is a decision, not
   * a fault, and `error` is reserved for faults so the stranded-source alert
   * stays meaningful. The copy says "asked Google to revoke", never
   * "revoked": the local revocation is immediate and certain, while the
   * Google-side call is a BEST-EFFORT background job so a provider outage
   * cannot block the person's disconnect.
   */
  /**
   * Mint the Telegram deep link and SHOW it rather than navigate: the tap has
   * to happen inside Telegram, on whatever device the person has it on, so a
   * same-tab `location.assign` to `t.me` would strand a desktop browser on
   * Telegram's web landing page. An anchor opens it where Telegram is
   * installed; the raw link is there to copy to a phone.
   */
  async function linkTelegram() {
    setError(null);
    setNotice(null);
    setLinkingTelegram(true);
    const result = await requestTelegramLink();
    setLinkingTelegram(false);
    if (!result.ok) {
      setError(telegramLinkRefusalCopy(result.error));
      return;
    }
    setTelegramLink({ link: result.link, expiresInSeconds: result.expiresInSeconds });
  }

  async function disconnectDrive() {
    setError(null);
    setNotice(null);
    setDisconnecting(true);
    const result = await submitCommand(workspaceId, "disconnect_account", {});
    setDisconnecting(false);

    if (!result.ok) {
      setError(settingsRefusalCopy(result.error, result.status));
      return;
    }
    setNotice(
      "Disconnected. Every folder is paused and access is revoked here — we have also asked Google to revoke it on their side. Your folders and everything already synced stay where they are.",
    );
    router.refresh();
  }

  /**
   * Sync ONE source, now.
   *
   * `sync-media` was a dead BFF path; this is the `sync_now` command. It is
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
    setError(null);
    setNotice(null);
    setSyncingId(sourceId);
    const result = await submitCommand(workspaceId, "sync_now", {
      source_id: sourceId,
    });
    setSyncingId(null);

    if (!result.ok) {
      setError(settingsRefusalCopy(result.error, result.status));
      return;
    }
    setNotice(
      result.data?.sync === "already_pending"
        ? "A sync is already queued for that folder."
        : "Sync started.",
    );
    router.refresh();
  }

  return (
    <div className="space-y-6 pt-4">
      {error && (
        <div className="mb-4 rounded-md border border-red-200 bg-red-50 p-3 text-sm text-red-800">
          {error}
        </div>
      )}
      {notice && (
        <div className="mb-4 rounded-md border bg-muted/40 p-3 text-sm">{notice}</div>
      )}
      <Card>
        <CardHeader>
          <CardTitle className="text-base">Telegram</CardTitle>
        </CardHeader>
        <CardContent className="space-y-3">
          {telegramLinked ? (
            <div className="flex items-center gap-2">
              <Badge variant="secondary" className="bg-green-100 text-green-800">
                Linked
              </Badge>
              <p className="text-sm text-muted-foreground">
                {telegramDisplayName
                  ? `Telegram account "${telegramDisplayName}" is linked to your Storydump account.`
                  : "A Telegram account is linked to your Storydump account."}{" "}
                If that is not you, contact us — there is no unlink control yet.
              </p>
            </div>
          ) : (
            <>
              <p className="text-sm text-muted-foreground">
                Link your Telegram account to approve posts and receive
                notifications there. Linking is per person, not per workspace,
                and the link below works once and expires after{" "}
                {telegramLink?.expiresInSeconds
                  ? Math.round(telegramLink.expiresInSeconds / 60)
                  : 15}{" "}
                minutes.
              </p>
              {telegramLink ? (
                <div className="space-y-2">
                  <Button asChild>
                    <a href={telegramLink.link} target="_blank" rel="noopener noreferrer">
                      Open Telegram to finish linking
                    </a>
                  </Button>
                  <p className="break-all font-mono text-xs text-muted-foreground">
                    {telegramLink.link}
                  </p>
                  <p className="text-xs text-muted-foreground">
                    <strong>Do not share this link.</strong> Whoever taps it links
                    their Telegram to your account. Tap Start in the chat that
                    opens — the bot confirms in the chat — then reload this page; it shows
                    Linked once the bot has heard from you. Asking for a new link
                    retires this one.
                  </p>
                  <Button variant="ghost" size="sm" onClick={linkTelegram} disabled={linkingTelegram}>
                    {linkingTelegram ? "Preparing link..." : "Get a new link"}
                  </Button>
                </div>
              ) : (
                <Button variant="outline" onClick={linkTelegram} disabled={linkingTelegram}>
                  {linkingTelegram ? "Preparing link..." : "Link Telegram"}
                </Button>
              )}
            </>
          )}
          <div className="border-t pt-3">
            <p className="text-sm font-medium">Telegram groups</p>
            <p className="mt-1 text-xs text-muted-foreground">
              Approval cards and notices for this workspace go to every group listed here.
              Adding one opens Telegram&apos;s group picker; the group you choose is bound to
              this workspace. A group can belong to one workspace only.
            </p>
            {bindings === null ? (
              <p className="mt-2 text-sm text-muted-foreground">
                Bound groups could not be loaded just now. Reload to try again.
              </p>
            ) : boundGroups.length > 0 ? (
              <ul className="mt-2 space-y-1 text-sm">
                {boundGroups.map((b) => (
                    <li key={b.id} className="flex items-center gap-2">
                      <Badge variant="secondary" className="bg-green-100 text-green-800">
                        Bound
                      </Badge>
                      <span className="text-muted-foreground">
                        {b.channel === "telegram_dm" ? "Direct chat" : "Group chat"} · id {b.external_ref}
                      </span>
                    </li>
                  ))}
              </ul>
            ) : (
              <p className="mt-2 text-sm text-muted-foreground">No Telegram group is bound yet.</p>
            )}
            {groupLinkError && <p className="mt-2 text-sm text-red-700">{groupLinkError}</p>}
            {groupLink ? (
              <div className="mt-3 space-y-2">
                <Button asChild>
                  <a href={groupLink.link} target="_blank" rel="noopener noreferrer">
                    Open Telegram to choose a group
                  </a>
                </Button>
                <p className="break-all font-mono text-xs text-muted-foreground">{groupLink.link}</p>
                <p className="text-xs text-muted-foreground">
                  Only you can use this link, from the Telegram account linked to your Storydump
                  user. It works once and expires after{" "}
                  {Math.round(groupLink.expiresInSeconds / 60)} minutes; the bot confirms in the
                  group, then reload this page. If the bot is already in the group and nothing
                  arrives, send this in the group instead:{" "}
                  <code className="break-all">{startCommandFor(groupLink.link)}</code>
                </p>
                <Button variant="ghost" size="sm" onClick={addTelegramGroup} disabled={mintingGroupLink}>
                  {mintingGroupLink ? "Preparing link..." : "Get a new link"}
                </Button>
              </div>
            ) : (
              <Button variant="outline" className="mt-3" onClick={addTelegramGroup} disabled={mintingGroupLink}>
                {mintingGroupLink ? "Preparing link..." : "Add a Telegram group"}
              </Button>
            )}
          </div>
        </CardContent>
      </Card>
      <Card>
        <CardHeader>
          <CardTitle className="text-base">Google Drive</CardTitle>
        </CardHeader>
        <CardContent className="space-y-4">
          {drive === null ? (
            <p className="text-sm text-muted-foreground">
              The Google Drive connection could not be loaded just now. Reload to try again.
            </p>
          ) : (
            <div className="flex flex-wrap items-center justify-between gap-3">
              <div className="flex flex-wrap items-center gap-2">
                {/* GREEN BELONGS TO THE GRANT ALONE: `driveStatusBadge` is
                    pinned so only `active` ever carries this tone. */}
                <Badge
                  variant="secondary"
                  className={
                    grant.tone === "active"
                      ? "bg-green-100 text-green-800"
                      : grant.tone === "attention"
                        ? "bg-amber-100 text-amber-900"
                        : "bg-muted text-muted-foreground"
                  }
                >
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

          <div className="space-y-2 border-t pt-4">
            <div className="flex items-center justify-between gap-3">
              <p className="text-sm font-medium">Folders</p>
              <Button size="sm" variant="outline" onClick={openPicker} disabled={!driveActive}>
                Add folder
              </Button>
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
                        <p className="font-medium">{source.folder_name ?? "Drive folder"}</p>
                        {/* The source's own operating state, coloured only
                            when it is a problem: healthy is unremarkable. */}
                        {source.state !== "active" && (
                          <Badge variant="secondary" className="bg-amber-100 text-amber-900">
                            {source.state}
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
                        onClick={() => removeFolder(source.id)}
                        disabled={removingId !== null}
                      >
                        {removingId === source.id ? "Removing..." : "Remove"}
                      </Button>
                    </div>
                  </li>
                ))}
              </ul>
            )}
            <p className="text-xs text-muted-foreground">
              Removing a folder stops syncing it; what was already synced stays. Disconnecting
              Google Drive pauses every folder and revokes access here, and asks Google to revoke
              it on their side.
            </p>
          </div>
        </CardContent>
      </Card>

      <Dialog
        open={pickerOpen}
        onOpenChange={(open) => {
          if (!open) closePicker();
        }}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Pick a Drive folder</DialogTitle>
            <DialogDescription>
              Storydump syncs the images and videos in the folder you pick. Open a folder to look
              inside it.
            </DialogDescription>
          </DialogHeader>
          <div className="flex gap-2">
            <Button
              size="sm"
              variant={pickerRoot === "mine" ? "default" : "outline"}
              onClick={() => switchRoot("mine")}
              disabled={pickerLoading}
            >
              My Drive
            </Button>
            <Button
              size="sm"
              variant={pickerRoot === "shared" ? "default" : "outline"}
              onClick={() => switchRoot("shared")}
              disabled={pickerLoading}
            >
              Shared with me
            </Button>
          </div>
          <div className="flex flex-wrap items-center gap-1 text-sm">
            <button
              type="button"
              className="underline-offset-2 hover:underline"
              onClick={() => goTo(-1)}
            >
              {pickerRoot === "shared" ? "Shared with me" : "My Drive"}
            </button>
            {pickerStack.map((f, i) => (
              <span key={f.id} className="flex items-center gap-1">
                <span className="text-muted-foreground">›</span>
                <button
                  type="button"
                  className="underline-offset-2 hover:underline"
                  onClick={() => goTo(i)}
                >
                  {f.name}
                </button>
              </span>
            ))}
          </div>
          {pickerError && <p className="text-sm text-red-700">{pickerError}</p>}
          {pickerTruncated && (
            <p className="text-xs text-muted-foreground">
              Showing the first folders alphabetically — this level has more. Open a folder to
              narrow the list.
            </p>
          )}
          <div className="max-h-72 overflow-y-auto rounded-md border">
            {pickerLoading ? (
              <p className="p-3 text-sm text-muted-foreground">Loading folders...</p>
            ) : pickerFolders !== null && pickerFolders.length === 0 ? (
              <p className="p-3 text-sm text-muted-foreground">No folders inside this one.</p>
            ) : (
              (pickerFolders ?? []).map((f) => (
                <div
                  key={f.id}
                  className="flex items-center justify-between gap-2 border-b px-3 py-2 last:border-b-0"
                >
                  <button
                    type="button"
                    className="min-w-0 flex-1 truncate text-left text-sm hover:underline"
                    onClick={() => enterFolder(f)}
                  >
                    {f.name}
                  </button>
                  <Button
                    size="sm"
                    variant="outline"
                    onClick={() => pickFolder(f)}
                    disabled={pickingId !== null}
                  >
                    {pickingId === f.id ? "Adding..." : "Use this folder"}
                  </Button>
                </div>
              ))
            )}
          </div>
          <DialogFooter>
            {pickerCurrent && (
              <Button onClick={() => pickFolder(pickerCurrent)} disabled={pickingId !== null}>
                {pickingId === pickerCurrent.id ? "Adding..." : `Use "${pickerCurrent.name}"`}
              </Button>
            )}
            <Button variant="ghost" onClick={closePicker}>
              Cancel
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <Card>
        <CardHeader>
          <CardTitle className="text-base">Media</CardTitle>
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="space-y-0.5">
            <p className="font-medium">{settings.media_count} media files</p>
            {/* `media_sync_enabled` has no source. Rendering the old
                `? "enabled" : "disabled"` here stated a fact about the
                workspace drawn from a missing column. */}
            <p className="text-sm text-muted-foreground">
              {settings.media_sync_enabled === null
                ? "Auto-sync state is not available from this API yet."
                : settings.media_sync_enabled
                  ? "Auto-sync enabled"
                  : "Auto-sync disabled"}
            </p>
          </div>
        </CardContent>
      </Card>
    </div>
  );
}
