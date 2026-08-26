"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Separator } from "@/components/ui/separator";
import { settingsRefusalCopy, submitSettingsChange } from "@/lib/command-client";
import type { SettingsView } from "@/lib/dashboard-payloads";
import { CategoryMixCard } from "./category-mix-card";
import { CaptionStyleCard } from "./caption-style-card";
import { RepostCadenceCard } from "./repost-cadence-card";

/**
 * ── The writes here now reach the port ─────────────────────────────────
 *
 * They used to be `postApi("schedule")` and `postApi("toggle-setting")`,
 * proxied to `POST /api/v1/workspaces/{ws}/<path>` — routes that do not exist
 * (#1057). Both are now one `settings_change` on the command client, which
 * carries the `Idempotency-Key` the port requires and mints a fresh submission
 * identity per save.
 *
 * `editable` is unchanged by this and is NOT this file's to decide. It is the
 * screen-level gate, held in `settings/page.tsx`, and it is being separated
 * from the removed-control case in its own change. Everything here is written
 * to be correct in BOTH of its states: false, and the true it becomes.
 *
 * ── Not every toggle on this screen is a `settings_change` ──────────────
 *
 * Three of the seven are (`dry_run_mode`, `enable_ai_captions`, and the one
 * this tier calls `enable_instagram_api`, which the workspace row calls
 * `api_publishing_enabled`). Three others have no source on the target tier at
 * all and already render `Unavailable` rather than a switch.
 *
 * The seventh, `is_paused`, is the one to be careful with: it READS fine, so
 * it draws a real switch showing a real value, but it is not in the port's
 * settings allowlist — pausing is `pause_workspace` / `resume_workspace`, two
 * separate commands this tier does not yet offer. Wiring it to
 * `settings_change` would send a key the port refuses BY NAME. So it carries
 * no key, stays inert, and says why. It is not a `settings_change` control and
 * is out of scope for the change that wired the other three.
 */

const HOUR_OPTIONS = Array.from({ length: 24 }, (_, i) => ({
  value: String(i),
  label: i === 0 ? "12 AM" : i < 12 ? `${i} AM` : i === 12 ? "12 PM" : `${i - 12} PM`,
}));

type ToggleKey =
  | "is_paused"
  | "dry_run_mode"
  | "enable_instagram_api"
  | "enable_ai_captions"
  | "show_verbose_notifications"
  | "send_lifecycle_notifications"
  | "media_sync_enabled";

/**
 * `settingsKey` is the column name the PORT accepts, or null when this toggle
 * is not a `settings_change` at all. It is what decides whether a switch is
 * interactive, so the mapping is stated once here rather than inferred at the
 * call site — and a toggle added without one is inert by default rather than
 * live and refused.
 */
const TOGGLES: {
  key: ToggleKey;
  label: string;
  description: string;
  settingsKey: string | null;
  /** Shown instead of the switch being silently dead. Required when null. */
  inertReason?: string;
}[] = [
  {
    key: "is_paused",
    label: "Pause Posting",
    description: "Temporarily stop all scheduled posts",
    // Not in the port's settings allowlist: pausing is its own command pair.
    settingsKey: null,
    inertReason: "Pausing is not wired up yet",
  },
  {
    key: "dry_run_mode",
    label: "Dry Run Mode",
    description: "Simulate posting without publishing",
    settingsKey: "dry_run_mode",
  },
  {
    key: "enable_instagram_api",
    label: "Instagram API",
    description: "Use Instagram API for direct posting",
    // Renamed once at the read seam (`dashboard-payloads.ts`); the port's name
    // is what goes on the wire.
    settingsKey: "api_publishing_enabled",
  },
  {
    key: "enable_ai_captions",
    label: "AI Captions",
    description: "Auto-generate captions with Claude",
    settingsKey: "enable_ai_captions",
  },
  // The three below have no source on the target tier, so they never draw a
  // switch at all — `settings[key]` is null and renders `Unavailable`.
  {
    key: "show_verbose_notifications",
    label: "Verbose Notifications",
    description: "Show detailed Telegram notifications",
    settingsKey: null,
    inertReason: "No source on this API yet",
  },
  {
    key: "send_lifecycle_notifications",
    label: "Lifecycle Notifications",
    description: "Receive startup/shutdown messages from the worker",
    settingsKey: null,
    inertReason: "No source on this API yet",
  },
  {
    key: "media_sync_enabled",
    label: "Media Sync",
    description: "Auto-sync media from connected sources",
    settingsKey: null,
    inertReason: "No source on this API yet",
  },
];

/** A value with no source is stated as such, never drawn as an off switch. */
function Unavailable() {
  return (
    <span className="text-sm text-muted-foreground">Not available yet</span>
  );
}

export function GeneralTab({
  settings,
  workspaceId,
  editable,
}: {
  settings: SettingsView;
  workspaceId: string;
  editable: boolean;
}) {
  const router = useRouter();
  const [postsPerDay, setPostsPerDay] = useState(settings.posts_per_day ?? 0);
  const [hoursStart, setHoursStart] = useState(
    settings.posting_hours_start === null ? "" : String(settings.posting_hours_start),
  );
  const [hoursEnd, setHoursEnd] = useState(
    settings.posting_hours_end === null ? "" : String(settings.posting_hours_end),
  );
  const [savingSchedule, setSavingSchedule] = useState(false);
  const [error, setError] = useState<string | null>(null);

  /**
   * The switch positions, which move optimistically and are put back if the
   * port refuses. Seeded from the server values; only the toggles that have a
   * value at all are ever read from here.
   */
  const [toggleState, setToggleState] = useState<Partial<Record<ToggleKey, boolean>>>(
    () =>
      Object.fromEntries(
        TOGGLES.map((t) => [t.key, settings[t.key]]).filter(
          ([, v]) => typeof v === "boolean",
        ),
      ),
  );
  const [togglingKey, setTogglingKey] = useState<ToggleKey | null>(null);

  /**
   * All three schedule fields in ONE command, deliberately.
   *
   * They are saved by a single button and the port validates the map as a
   * unit, so splitting them into three commands would make a refusal of the
   * end hour leave the other two already written — a half-applied schedule
   * with no way to name what happened.
   */
  async function saveSchedule() {
    setError(null);
    setSavingSchedule(true);
    const result = await submitSettingsChange(workspaceId, {
      posts_per_day: postsPerDay,
      posting_hours_start: Number(hoursStart),
      posting_hours_end: Number(hoursEnd),
    });
    setSavingSchedule(false);

    if (!result.ok) {
      setError(settingsRefusalCopy(result.error));
      return;
    }
    // Re-read rather than keep the submitted values on screen. This card is
    // not the only thing rendered from `settings`, and a write that updated
    // only the boxes it was typed into would leave the rest of the tab showing
    // pre-write state with nothing marking the difference.
    router.refresh();
  }

  /**
   * A toggle sends the value it is moving TO. The dead path it replaces sent
   * only `setting_name` and let the server flip whatever it found, which is
   * not expressible as a `settings_change` and is a worse contract anyway: two
   * clicks racing would flip twice from a state neither of them read.
   */
  async function toggle(key: ToggleKey, settingsKey: string, next: boolean) {
    setError(null);
    setTogglingKey(key);
    const previous = toggleState[key];
    setToggleState((prev) => ({ ...prev, [key]: next }));

    const result = await submitSettingsChange(workspaceId, { [settingsKey]: next });
    setTogglingKey(null);

    if (!result.ok) {
      // Put the switch back where it was. Leaving it on the new position
      // beside an error message shows a state the workspace is not in.
      setToggleState((prev) => ({ ...prev, [key]: previous }));
      setError(settingsRefusalCopy(result.error));
      return;
    }
    router.refresh();
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
          <CardTitle className="text-base">Posting Schedule</CardTitle>
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="grid gap-4 sm:grid-cols-3">
            <div className="space-y-2">
              <Label htmlFor="posts-per-day">Posts per day</Label>
              {settings.posts_per_day === null ? (
                <Unavailable />
              ) : (
                <Input
                  id="posts-per-day"
                  type="number"
                  min={1}
                  max={50}
                  value={postsPerDay}
                  disabled={!editable}
                  onChange={(e) => setPostsPerDay(Number(e.target.value))}
                />
              )}
            </div>
            <div className="space-y-2">
              <Label>Start hour</Label>
              {settings.posting_hours_start === null ? (
                <Unavailable />
              ) : (
                <Select value={hoursStart} onValueChange={setHoursStart} disabled={!editable}>
                  <SelectTrigger className="w-full">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    {HOUR_OPTIONS.map((h) => (
                      <SelectItem key={h.value} value={h.value}>
                        {h.label}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              )}
            </div>
            <div className="space-y-2">
              <Label>End hour</Label>
              {settings.posting_hours_end === null ? (
                <Unavailable />
              ) : (
                <Select value={hoursEnd} onValueChange={setHoursEnd} disabled={!editable}>
                  <SelectTrigger className="w-full">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    {HOUR_OPTIONS.map((h) => (
                      <SelectItem key={h.value} value={h.value}>
                        {h.label}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              )}
            </div>
          </div>
          {editable && (
            <Button onClick={saveSchedule} disabled={savingSchedule}>
              {savingSchedule ? "Saving..." : "Save Schedule"}
            </Button>
          )}
        </CardContent>
      </Card>

      <CaptionStyleCard
        captionStyle={settings.caption_style}
        workspaceId={workspaceId}
        editable={editable}
        onError={setError}
      />

      {/*
        CategoryMixCard is omitted while read-only rather than rendered empty:
        it FETCHES its own data from `postApi("category-mix")`, another route
        that does not exist, so rendering it would put an error banner on a
        screen whose whole point is that what it shows is true.

        DO NOT LET P3'S FLIP RESTORE THIS ONE. Every other control behind
        `editable` is pending on P3 — a settings_change or sync_now target
        that already has a built executor, so flipping the flag is exactly
        what completes them. This card is pending on DIFFERENT work and
        would come back broken:
          - its READ is a POST to a route that does not exist. navi
            confirmed a genuine read/write split upstream
            (`get_current_mix_as_dict` vs `set_mix`), so it becomes a GET —
            that is epic P5, not P3.
          - its WRITE (`update-category-mix`) has no target-tier home at
            all: no vocabulary entry and no settings column. An open GAP.
        So it needs its own condition before `editable` is flipped, and a
        reviewer of P3 should treat this line as a blocker rather than a
        beneficiary of that change.
      */}
      {editable && <CategoryMixCard />}

      <RepostCadenceCard
        repostTtlDays={settings.repost_ttl_days}
        skipTtlDays={settings.skip_ttl_days}
        workspaceId={workspaceId}
        editable={editable}
        onError={setError}
      />

      <Card>
        <CardHeader>
          <CardTitle className="text-base">Toggles</CardTitle>
        </CardHeader>
        <CardContent className="space-y-4">
          {TOGGLES.map((row, i) => {
            const value = settings[row.key];
            const position = toggleState[row.key] ?? value;
            // Two independent reasons a switch does not move, and they are not
            // the same fact: the screen is read-only, or this particular
            // setting has no command behind it.
            const wired = row.settingsKey !== null;
            return (
              <div key={row.key}>
                {i > 0 && <Separator className="mb-4" />}
                <div className="flex items-center justify-between gap-4">
                  <div className="space-y-0.5">
                    <Label>{row.label}</Label>
                    <p className="text-sm text-muted-foreground">
                      {row.description}
                    </p>
                  </div>
                  {/*
                    A null here is a MISSING COLUMN, not an off switch. Drawing
                    it as unchecked would state that the feature is off for this
                    workspace, which nothing establishes.
                  */}
                  {value === null || typeof position !== "boolean" ? (
                    <Unavailable />
                  ) : (
                    <div className="flex items-center gap-3">
                      {/*
                        Said only where it is the ACTIVE reason. While the whole
                        screen is read-only, every switch is inert and naming
                        one of them specially would imply the others are fine.
                      */}
                      {editable && !wired && row.inertReason && (
                        <span className="text-sm text-muted-foreground">
                          {row.inertReason}
                        </span>
                      )}
                      <Switch
                        checked={position}
                        disabled={!editable || !wired || togglingKey !== null}
                        aria-label={row.label}
                        onCheckedChange={
                          wired
                            ? (next) => toggle(row.key, row.settingsKey!, next)
                            : undefined
                        }
                      />
                    </div>
                  )}
                </div>
              </div>
            );
          })}
        </CardContent>
      </Card>
    </div>
  );
}
