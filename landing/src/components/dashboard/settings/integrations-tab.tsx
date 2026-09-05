"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { sourceCredentialBadge } from "@/lib/source-credential";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import type { SettingsView, SourceRow } from "@/lib/dashboard-payloads";
import {
  addDriveSource,
  addSourceRefusalCopy,
  connectRefusalCopy,
  requestSourceConnect,
} from "@/lib/source-connect";
import { requestTelegramLink, telegramLinkRefusalCopy } from "@/lib/telegram-link";
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
 * `sync-media` and `disconnect-gdrive` are real controls whose routes are not
 * wired yet (#1063 / epic P6, F5 locked (b)) — DISABLED WITH A REASON, not
 * removed: a control that is real and is coming back stays visible and inert.
 *
 * ── The Connect button is BACK, and per SOURCE this time (#1065) ────────────
 *
 * #1070 deleted the old one because it was the wrong shape, not merely
 * unwired: `openOAuthWindow` called `oauth-url/<provider>`, which is
 * per-WORKSPACE, and a Drive credential is per-SOURCE
 * (`ck_credentials_one_owner` ties it to a `media_source_id`). There is no
 * per-workspace answer to "connect Drive", because a workspace holds one
 * source per Drive folder — `get_or_create_media_source` is idempotent on the
 * FOLDER and says in as many words that creates for different folders never
 * contend. So the card is now a LIST, one row per source, and each row's
 * button carries that row's id.
 *
 * ── What this card can and cannot say ──────────────────────────────────────
 *
 * When this card was built, `GET /workspaces/{ws}/sources` returned no
 * credential field at all, so this tier could not tell a source that had been
 * granted from one that had not. Two things follow from that, and both are
 * still what the code does:
 *
 * 1. The button's label is NEUTRAL across connect and reconnect, because a
 *    precise one would have been a guess. The route disambiguates for itself.
 * 2. The old "Connected" heading is GONE. It was `drive !== null` — a source
 *    ROW existing — so a folder added and never granted rendered as
 *    "Connected" with a green `active` badge, which is a claim this tier had
 *    no way to make. What is shown is what is known.
 *
 * **That gap is now CLOSED at the API and not yet consumed here.** #1080 added
 * `credential_status` to the payload — `none` | `active` | `expired` |
 * `revoked` — deriving it rather than passing `state` through, precisely
 * because `media_sources.state` cannot answer "is this connected" (#1078: a
 * source created and never credentialed is `active` too). `none` versus the
 * last two are different user actions, connect versus reconnect, which is the
 * distinction this card could not previously make.
 *
 * `SourceRow` in this tier does not declare the field yet and nothing here
 * reads it. Consuming it is its own change: a precise label, and a heading
 * that can say "Connected" truthfully for the first time.
 */
export function IntegrationsTab({
  settings,
  sources,
  workspaceId,
  telegramLinked,
  telegramDisplayName,
}: {
  settings: SettingsView;
  /** The workspace's sources, unflattened — this card renders them per row. */
  sources: SourceRow[];
  workspaceId: string;
  /** Whether the signed-in USER has a Telegram identity attached — a fact
   *  about the person, not this workspace (#1172 clause 1). */
  telegramLinked: boolean;
  /** Who that identity is, so a link tapped by the wrong person is visible. */
  telegramDisplayName: string | null;
}) {
  const router = useRouter();
  const [telegramLink, setTelegramLink] = useState<{ link: string; expiresInSeconds: number } | null>(null);
  const [linkingTelegram, setLinkingTelegram] = useState(false);
  const [syncingId, setSyncingId] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [disconnectingId, setDisconnectingId] = useState<string | null>(null);
  const [connectingId, setConnectingId] = useState<string | null>(null);
  const [folderRef, setFolderRef] = useState("");
  const [addingFolder, setAddingFolder] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const driveSources = sources.filter((s) => s.provider === "gdrive");
  // Only shown when there is something to tell apart. The sources payload
  // carries no folder name (`config` is not returned), so with two rows the
  // honest disambiguator is a short id; with one it would be noise.
  const needsIdentifier = driveSources.length > 1;

  /**
   * Start the grant for ONE source, then hand the browser to Google.
   *
   * `window.location.assign`, not a new tab. The old per-workspace button
   * opened a popup and polled `visibilitychange` to notice the return; the
   * callback is a real navigation (`GET /auth/google-drive/callback`) that
   * lands back in this app, so a same-tab redirect needs no listener and no
   * guessing about when the person came back. #1070 deleted those listeners
   * with the button that needed them; this does not bring them back.
   */
  /**
   * Add a Drive folder as a source.
   *
   * This closes the dead end #1077 left stated on this screen: the connect
   * route is per-source, so a workspace with no source had no way to get one
   * and therefore no button — and the only control that could create one lived
   * in the setup wizard, which nothing renders (`/dashboard/setup` was deleted
   * and `sidebar.tsx` says so).
   *
   * The confirmation says what the route actually returns. The control this
   * replaces rendered "N files found" and a category list from the legacy
   * route; `POST /sources` deliberately does not read the folder — that is the
   * Drive seam and a separate build — so those numbers do not exist yet and
   * claiming them would be inventing them.
   */
  async function addFolder() {
    setError(null);
    setNotice(null);
    setAddingFolder(true);
    const result = await addDriveSource(workspaceId, folderRef);
    setAddingFolder(false);

    if (!result.ok) {
      setError(addSourceRefusalCopy(result.error));
      return;
    }
    setFolderRef("");
    // `created` matters to a person: submitting the same folder twice returns
    // the SAME source rather than making a second one, and saying "added"
    // both times would hide that.
    setNotice(
      result.created
        ? "Folder added. Set up Google access for it to start syncing."
        : "That folder is already a source here.",
    );
    router.refresh();
  }

  async function connect(sourceId: string) {
    setError(null);
    setConnectingId(sourceId);
    const result = await requestSourceConnect(workspaceId, sourceId);
    if (!result.ok) {
      setConnectingId(null);
      setError(connectRefusalCopy(result.error));
      return;
    }
    // Deliberately no `setConnectingId(null)` on success: the page is leaving.
    // Clearing it would flash the button back to its resting label during the
    // navigation, which reads as the click having done nothing.
    window.location.assign(result.authorizationUrl);
  }
  /**
   * Disconnect a Drive source — REVOKE AND PAUSE, never a delete.
   *
   * The executor revokes the credential, KEEPS the row, and sets the source
   * `paused` rather than `error` (`command_executors.py:405`): a disconnect is
   * a decision, not a fault, and `error` is reserved for faults so the stranded
   * -source alert stays meaningful. Nothing is deleted — `oauth_credentials`
   * cascades from the source, so removing it would erase an audit trail.
   *
   * The button is styled destructive because it withdraws access, and the copy
   * beside it says what it does NOT do; a control that looks like data loss is
   * one people will not press, and an unpressed disconnect is the same
   * public-commitment gap one step later.
   *
   * The copy says "asked Google to revoke", never "revoked", and the distinction
   * is the mechanism's not a hedge: the local revocation is immediate and
   * certain, while the Google-side call is a BEST-EFFORT background job and is
   * deliberately not performed here (`command_executors.py:419`) so a provider
   * outage cannot block the user's disconnect. Claiming it has happened would be
   * a misleadingly-specific promise where an honestly-generic one is available.
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

  async function disconnectSource(sourceId: string) {
    setError(null);
    setNotice(null);
    setDisconnectingId(sourceId);
    const result = await submitCommand(workspaceId, "disconnect_account", {
      source_id: sourceId,
    });
    setDisconnectingId(null);

    if (!result.ok) {
      setError(settingsRefusalCopy(result.error, result.status));
      return;
    }
    setNotice(
      "Disconnected. Syncing is paused and access is revoked here — we have also asked Google to revoke it on their side. Your folder and everything already synced stay where they are.",
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
        </CardContent>
      </Card>
      <Card>
        <CardHeader>
          <CardTitle className="text-base">Google Drive</CardTitle>
        </CardHeader>
        <CardContent>
          {driveSources.length === 0 ? (
            <p className="py-2 text-sm text-muted-foreground">
              No Google Drive folder is set up for this workspace yet. Add one
              below.
            </p>
          ) : (
            <ul className="divide-y">
              {driveSources.map((source) => (
                <li
                  key={source.id}
                  className="flex flex-wrap items-center justify-between gap-3 py-3 first:pt-0 last:pb-0"
                >
                  <div className="space-y-0.5">
                    <div className="flex items-center gap-2">
                      <p className="font-medium">
                        Drive folder
                        {needsIdentifier && (
                          <span className="ml-2 font-mono text-xs text-muted-foreground">
                            {source.id.slice(0, 8)}
                          </span>
                        )}
                      </p>
                      {/* The source's own state, not a hardcoded badge:
                          erroring and healthy are different facts with
                          different remedies. */}
                      {/* GREEN BELONGS TO THE CREDENTIAL, NEVER TO `state`.
                          `state` is NOT NULL DEFAULT 'active' (054), so a folder
                          added and never granted is `active` — colouring that
                          green is what told the first real user an unauthorised
                          folder was connected. `state` still renders, because it
                          answers a real question (is this source operating), but
                          it is unremarkable when normal and only coloured when
                          it is a problem. */}
                      {(() => {
                        const cred = sourceCredentialBadge(source.credential_status);
                        return (
                          <Badge
                            variant="secondary"
                            className={
                              cred.tone === "active"
                                ? "bg-green-100 text-green-800"
                                : cred.tone === "attention"
                                  ? "bg-amber-100 text-amber-900"
                                  : "bg-muted text-muted-foreground"
                            }
                          >
                            {cred.label}
                          </Badge>
                        );
                      })()}
                      <Badge
                        variant="secondary"
                        className={
                          source.state === "active"
                            ? "bg-muted text-muted-foreground"
                            : "bg-amber-100 text-amber-900"
                        }
                      >
                        {source.state}
                      </Badge>
                    </div>
                    {/*
                      This line says what SYNC has done; the badge above says
                      whether we can reach Google at all. Two questions, two
                      answers — collapsing them is how "added" came to read as
                      "connected".

                      The comment that stood here said `SourceRow` did not
                      declare `credential_status` and nothing read it. The first
                      half had already stopped being true; the second is what
                      this change fixes.
                    */}
                    <p className="text-sm text-muted-foreground">
                      {source.last_sync_success_at
                        ? "Syncing"
                        : "No sync has completed yet"}
                    </p>
                  </div>
                  <div className="flex flex-col items-end gap-1">
                    <div className="flex items-center gap-2">
                      <Button
                        size="sm"
                        onClick={() => connect(source.id)}
                        disabled={connectingId !== null}
                      >
                        {connectingId === source.id
                          ? "Opening Google..."
                          : "Set up Google access"}
                      </Button>
                      {/* Neither is behind `editable` now: both `sync_now` and
                          `disconnect_account` are built executors and these call
                          them. Nothing on this card is pending any more. */}
                      <Button
                        variant="outline"
                        size="sm"
                        onClick={() => syncSource(source.id)}
                        disabled={syncingId !== null}
                      >
                        {syncingId === source.id ? "Syncing..." : "Sync Now"}
                      </Button>
                      <Button
                        variant="destructive"
                        size="sm"
                        onClick={() => disconnectSource(source.id)}
                        disabled={disconnectingId !== null}
                      >
                        {disconnectingId === source.id
                          ? "Disconnecting..."
                          : "Disconnect"}
                      </Button>
                    </div>
                  </div>
                </li>
              ))}
            </ul>
          )}
          {driveSources.length > 0 && (
            <div className="space-y-1 pt-3">
              <p className="text-xs text-muted-foreground">
                Disconnecting pauses syncing and revokes access here, and asks Google
                to revoke it on their side. Your folder and everything already synced
                stay where they are.
              </p>
              {/* Once for the card, not once per row: the folder ref lives in
                  `media_sources.config`, which the sources route does not
                  return, so it is omitted rather than guessed. */}
              <p className="text-xs text-muted-foreground">
                The folder each source syncs from is not available from this API yet.
              </p>
            </div>
          )}
          <div className="space-y-2 border-t pt-4 mt-4">
            <Label htmlFor="folder-ref">Add a folder</Label>
            <div className="flex flex-wrap gap-2">
              <Input
                id="folder-ref"
                value={folderRef}
                onChange={(e) => setFolderRef(e.target.value)}
                placeholder="Paste the Drive folder link"
                className="max-w-md"
              />
              <Button onClick={addFolder} disabled={addingFolder || !folderRef.trim()}>
                {addingFolder ? "Adding..." : "Add folder"}
              </Button>
            </div>
            {/* What a valid reference IS belongs to the port, which takes a
                folder URL or a bare id and refuses anything else by name. This
                says the same thing in a sentence rather than re-implementing
                the rule as a second regex that can disagree with it. */}
            <p className="text-xs text-muted-foreground">
              Open the folder in Google Drive and copy the address, or paste its
              folder id.
            </p>
          </div>

        </CardContent>
      </Card>

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
