"use client";

import { useState } from "react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Notice } from "@/components/ui/notice";
import type {
  DriveStatus,
  SettingsView,
  SourceRow,
} from "@/lib/dashboard-payloads";
import type { ChannelBinding } from "@/lib/types";
import type { SourceMix } from "@/lib/source-mix";
import { DriveCard } from "./drive-card";
import { TelegramCard } from "./telegram-card";

/**
 * Integrations — Google Drive and Telegram, both live.
 *
 * Every action here is wired: Drive connect is the per-workspace grant (workspace Drive grant,
 *), disconnect is the `disconnect_account` command, and sync is
 * `sync_now`. Nothing on this tab is held off.
 *
 * The connection facts are real: `gdrive_connected` and the source's own
 * `state` come from `GET /workspaces/{ws}/sources`, and the media count from
 * `stats`. What has no source is stated as such rather than defaulted —
 * `mediaSyncEnabled ?? false` would render the flat sentence "Auto-sync
 * disabled", which is a claim about the workspace made from a column that
 * does not exist.
 *
 * ── Composition, like the two tabs beside it ─────────────────────
 *
 * Each card holds the state it reads — `TelegramCard`, `DriveCard` and
 * `DriveFolderPickerDialog` — as the cards of `general-tab.tsx` and
 * `api-tokens-tab.tsx` do.
 *
 * THE BANNER'S TWO LIVE HERE, and that is the whole reason this component
 * holds state. `error` and `notice` are written by both cards — a Drive
 * disconnect, a Telegram mint refusal and a folder pick all report through
 * them — and read in exactly one place, the pair of `<Notice>`s below. State
 * two cards share belongs to their parent; pushing a copy into each would
 * give the tab two banners that can disagree.
 */
export function IntegrationsTab({
  settings,
  sources,
  drive,
  mix,
  workspaceId,
  telegramLinked,
  bindings = [],
  telegramDisplayName,
  isAdmin,
}: {
  settings: SettingsView;
  /** The workspace's sources, unflattened — the Drive card renders them per row. */
  sources: SourceRow[];
  /** The workspace's Google Drive grant; null = could not be loaded. */
  drive: DriveStatus | null;
  /**
   * The posting mix as the page read it when the workspace is converted
   * (protocol 2), else null. Picking or removing a folder moves that mix, so
   * the Drive card writes against its revision.
   */
  mix: SourceMix | null;
  workspaceId: string;
  /** Whether the signed-in USER has a Telegram identity attached — a fact
   * about the person, not this workspace. */
  telegramLinked: boolean;
  /** The Telegram chats this WORKSPACE's cards go to; null = could not be loaded. */
  bindings?: ChannelBinding[] | null;
  /** Who that identity is, so a link tapped by the wrong person is visible. */
  telegramDisplayName: string | null;
  /** Admin or owner — gates removing a Telegram group. */
  isAdmin: boolean;
}) {
  const [notice, setNotice] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  return (
    <div className="space-y-6 pt-4">
      {error && (
        <Notice tone="error" className="mb-4">
          {error}
        </Notice>
      )}
      {notice && (
        <Notice tone="info" className="mb-4">
          {notice}
        </Notice>
      )}
      {/* `bindings` is passed straight through: `null` is NOT `[]` here. The
          card says "could not be loaded" for one and "none bound yet" for the
          other, so the only default is the parameter's own, above. */}
      <TelegramCard
        workspaceId={workspaceId}
        telegramLinked={telegramLinked}
        telegramDisplayName={telegramDisplayName}
        bindings={bindings}
        canRemoveGroups={isAdmin}
        onError={setError}
        onNotice={setNotice}
      />
      <DriveCard
        sources={sources}
        drive={drive}
        mix={mix}
        workspaceId={workspaceId}
        onError={setError}
        onNotice={setNotice}
      />

      <Card>
        <CardHeader>
          <CardTitle>Media</CardTitle>
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="space-y-0.5">
            <p className="font-medium">{settings.media_count} media files</p>
          </div>
        </CardContent>
      </Card>
    </div>
  );
}
