"use client";

import { useEffect, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { postApi, openOAuthWindow } from "@/lib/dashboard-api";
import type { SettingsView } from "@/lib/dashboard-payloads";

/**
 * Integrations, read-only (#1063).
 *
 * Every action on this tab targets a route that does not exist —
 * `disconnect-gdrive`, `sync-media`, and `oauth-url/google-drive` behind the
 * connect button. They are held off by `editable` rather than left to fail.
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
 * The Connect button is different in kind and is REMOVED. `openOAuthWindow`
 * calls `oauth-url/<provider>`, which is PER-WORKSPACE — and a Drive
 * credential is per-SOURCE (`ck_credentials_one_owner` ties it to a
 * `media_source_id`). So this control is not merely unwired, it is the WRONG
 * SHAPE: there is no per-workspace answer to "connect Drive", because a
 * workspace can hold more than one source. Leaving it disabled would suggest
 * the same button returns, and it does not.
 */
const DISABLED_REASON =
  "Not wired up yet — this action is not available on this API version.";

export function IntegrationsTab({
  settings,
  editable,
}: {
  settings: SettingsView;
  editable: boolean;
}) {
  const router = useRouter();
  const [syncing, setSyncing] = useState(false);
  const [disconnecting, setDisconnecting] = useState(false);
  const [connecting, setConnecting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const oauthPending = useRef(false);

  useEffect(() => {
    function onVisible() {
      if (document.visibilityState === "visible" && oauthPending.current) {
        oauthPending.current = false;
        router.refresh();
      }
    }
    document.addEventListener("visibilitychange", onVisible);
    return () => document.removeEventListener("visibilitychange", onVisible);
  }, [router]);

  async function connectGdrive() {
    setError(null);
    setConnecting(true);
    try {
      await openOAuthWindow("google-drive");
      oauthPending.current = true;
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to connect Google Drive");
    } finally {
      setConnecting(false);
    }
  }

  async function disconnectGdrive() {
    setError(null);
    setDisconnecting(true);
    try {
      await postApi("disconnect-gdrive");
      router.refresh();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to disconnect Google Drive");
    } finally {
      setDisconnecting(false);
    }
  }

  async function syncMedia() {
    setError(null);
    setSyncing(true);
    try {
      await postApi("sync-media");
      router.refresh();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to sync media");
    } finally {
      setSyncing(false);
    }
  }

  return (
    <div className="space-y-6 pt-4">
      {error && (
        <div className="mb-4 rounded-md border border-red-200 bg-red-50 p-3 text-sm text-red-800">
          {error}
        </div>
      )}
      <Card>
        <CardHeader>
          <CardTitle className="text-base">Google Drive</CardTitle>
        </CardHeader>
        <CardContent>
          {settings.gdrive_connected ? (
            <div className="flex items-center justify-between">
              <div className="space-y-0.5">
                <div className="flex items-center gap-2">
                  <p className="font-medium">Connected</p>
                  {/* The source's own state, not a hardcoded "Active" badge:
                      connected-but-erroring and connected-and-healthy are
                      different facts with different remedies. */}
                  {settings.media_source_state && (
                    <Badge
                      variant="secondary"
                      className={
                        settings.media_source_state === "active"
                          ? "bg-green-100 text-green-800"
                          : "bg-amber-100 text-amber-900"
                      }
                    >
                      {settings.media_source_state}
                    </Badge>
                  )}
                </div>
                {settings.gdrive_email && (
                  <p className="text-sm text-muted-foreground">
                    {settings.gdrive_email}
                  </p>
                )}
              </div>
              <div className="flex flex-col items-end gap-1">
                <div className="flex items-center gap-2">
                  <Button
                    variant="outline"
                    size="sm"
                    onClick={syncMedia}
                    disabled={!editable || syncing}
                    title={editable ? undefined : DISABLED_REASON}
                  >
                    {syncing ? "Syncing..." : "Sync Now"}
                  </Button>
                  <Button
                    variant="destructive"
                    size="sm"
                    onClick={disconnectGdrive}
                    disabled={!editable || disconnecting}
                    title={editable ? undefined : DISABLED_REASON}
                  >
                    {disconnecting ? "Disconnecting..." : "Disconnect"}
                  </Button>
                </div>
                {!editable && (
                  <p className="text-xs text-muted-foreground">
                    Not wired up yet
                  </p>
                )}
              </div>
            </div>
          ) : (
            <div className="py-4 text-center">
              <p className="text-sm text-muted-foreground mb-4">
                No Google Drive source is connected to this workspace.
              </p>
              {editable ? (
                <Button onClick={connectGdrive} disabled={connecting}>
                  {connecting ? "Connecting..." : "Connect Google Drive"}
                </Button>
              ) : (
                /*
                  Removed rather than shown-and-disabled: `openOAuthWindow`
                  calls `oauth-url/google-drive`, which is not a route on this
                  API (#1063). A button offering an action the app cannot
                  perform is the #1050 defect this screen is being made honest
                  about.
                */
                <p className="text-sm text-muted-foreground">
                  Connecting Google Drive is not available from this screen yet.
                </p>
              )}
            </div>
          )}
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
          {settings.media_source_type && (
            <div className="rounded-md border bg-muted/40 p-3 text-sm space-y-1">
              <div className="flex items-baseline gap-2">
                <span className="text-muted-foreground">Source</span>
                <span className="font-medium capitalize">
                  {settings.media_source_type.replace(/_/g, " ")}
                </span>
              </div>
              {/* The folder ref lives in `media_sources.config`, which the
                  sources route does not return — so it is omitted rather than
                  guessed from the provider. */}
              <p className="text-xs text-muted-foreground pt-1">
                The folder this syncs from is not available from this API yet.
              </p>
            </div>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
