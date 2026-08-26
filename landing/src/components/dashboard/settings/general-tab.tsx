"use client";

import { useState } from "react";
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
import { postApi } from "@/lib/dashboard-api";
import type { SettingsView } from "@/lib/dashboard-payloads";
import { CategoryMixCard } from "./category-mix-card";
import { CaptionStyleCard } from "./caption-style-card";
import { RepostCadenceCard } from "./repost-cadence-card";

/**
 * ── The write targets here are DEAD, and `editable` is what holds them off ──
 *
 * `postApi("schedule")` and `postApi("toggle-setting")` proxy to
 * `POST /api/v1/workspaces/{ws}/<path>`. Neither route exists (#1063). The
 * calls are kept so the shape of the screen is legible, and are unreachable
 * while `editable` is false.
 *
 * FLIPPING `editable` ALONE DOES NOT MAKE THIS WORK. The target tier takes
 * commands, not per-setting endpoints, and `Idempotency-Key` is required
 * server-side — so repointing the paths alone trades a 404 for a 400. The
 * shape these have to take already exists as a worked example:
 * `app/api/workspaces/[id]/commands/[command]/route.ts` (#1059), which mints
 * the key server-side and never lets the browser supply it. Mapping the six
 * onto that is #1063 option 2. Enabling the controls without it re-creates
 * precisely the harm this screen was changed to avoid.
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

const TOGGLES: { key: ToggleKey; label: string; description: string }[] = [
  { key: "is_paused", label: "Pause Posting", description: "Temporarily stop all scheduled posts" },
  { key: "dry_run_mode", label: "Dry Run Mode", description: "Simulate posting without publishing" },
  { key: "enable_instagram_api", label: "Instagram API", description: "Use Instagram API for direct posting" },
  { key: "enable_ai_captions", label: "AI Captions", description: "Auto-generate captions with Claude" },
  { key: "show_verbose_notifications", label: "Verbose Notifications", description: "Show detailed Telegram notifications" },
  { key: "send_lifecycle_notifications", label: "Lifecycle Notifications", description: "Receive startup/shutdown messages from the worker" },
  { key: "media_sync_enabled", label: "Media Sync", description: "Auto-sync media from connected sources" },
];

/** A value with no source is stated as such, never drawn as an off switch. */
function Unavailable() {
  return (
    <span className="text-sm text-muted-foreground">Not available yet</span>
  );
}

export function GeneralTab({
  settings,
  editable,
}: {
  settings: SettingsView;
  editable: boolean;
}) {
  const [postsPerDay, setPostsPerDay] = useState(settings.posts_per_day ?? 0);
  const [hoursStart, setHoursStart] = useState(
    settings.posting_hours_start === null ? "" : String(settings.posting_hours_start),
  );
  const [hoursEnd, setHoursEnd] = useState(
    settings.posting_hours_end === null ? "" : String(settings.posting_hours_end),
  );
  const [savingSchedule, setSavingSchedule] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function saveSchedule() {
    setError(null);
    setSavingSchedule(true);
    try {
      await postApi("schedule", {
        posts_per_day: postsPerDay,
        posting_hours_start: Number(hoursStart),
        posting_hours_end: Number(hoursEnd),
      });
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to save schedule");
    } finally {
      setSavingSchedule(false);
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

      <CaptionStyleCard captionStyle={settings.caption_style} editable={editable} onError={setError} />

      {/*
        CategoryMixCard is omitted while read-only rather than rendered empty:
        it FETCHES its own data from `postApi("category-mix")`, another route
        that does not exist, so rendering it would put an error banner on a
        screen whose whole point is that what it shows is true.
      */}
      {editable && <CategoryMixCard />}

      <RepostCadenceCard
        repostTtlDays={settings.repost_ttl_days}
        skipTtlDays={settings.skip_ttl_days}
        editable={editable}
        onError={setError}
      />

      <Card>
        <CardHeader>
          <CardTitle className="text-base">Toggles</CardTitle>
        </CardHeader>
        <CardContent className="space-y-4">
          {TOGGLES.map((toggle, i) => {
            const value = settings[toggle.key];
            return (
              <div key={toggle.key}>
                {i > 0 && <Separator className="mb-4" />}
                <div className="flex items-center justify-between">
                  <div className="space-y-0.5">
                    <Label>{toggle.label}</Label>
                    <p className="text-sm text-muted-foreground">
                      {toggle.description}
                    </p>
                  </div>
                  {/*
                    A null here is a MISSING COLUMN, not an off switch. Drawing
                    it as unchecked would state that the feature is off for this
                    workspace, which nothing establishes.
                  */}
                  {value === null ? (
                    <Unavailable />
                  ) : (
                    <Switch
                      checked={value}
                      disabled={!editable}
                      aria-label={toggle.label}
                    />
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
