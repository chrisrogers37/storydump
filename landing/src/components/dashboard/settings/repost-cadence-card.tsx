"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { settingsRefusalCopy, submitSettingsChange } from "@/lib/command-client";
import { settingField } from "@/lib/settings-defaults";

interface Props {
  workspaceId: string;
  repostTtlDays: number | null;
  skipTtlDays: number | null;
  /** The deployment's fallbacks, served on the config payload — never typed
   *  here. A copy in this file is what #1366 was. */
  defaults: { repost_ttl_days: number; skip_ttl_days: number };
  onError: (message: string | null) => void;
}

/**
 * Per-workspace repost and skip lock TTLs.
 *
 * NULL means "no workspace value — the deployment's defaults apply"
 * (`DEFAULT_REPOST_TTL_DAYS`, `DEFAULT_SKIP_TTL_DAYS`), which is not the same
 * as zero and not the same as any particular number. The second half of this
 * note used to describe `chat_settings` and migration 029 — a legacy table
 * this tier no longer reads.
 *
 * The fields SHOW that fallback, marked as a default, and it arrives on the
 * payload rather than being written here (#1366). Both halves matter: a copy
 * in this file drifted once already — it read 30 while a worker publish
 * locked for 7 (#1365) — and it was also the baseline for change-detection,
 * so with nothing stored, typing the default read as "no change" and could
 * never be saved.
 */
export function RepostCadenceCard({
  repostTtlDays,
  skipTtlDays,
  defaults,
  workspaceId,
  onError,
}: Props) {
  const router = useRouter();
  const repostField = settingField(repostTtlDays, defaults.repost_ttl_days);
  const skipField = settingField(skipTtlDays, defaults.skip_ttl_days);
  const [repost, setRepost] = useState<number>(repostField.value);
  const [skip, setSkip] = useState<number>(skipField.value);
  const [saving, setSaving] = useState<"repost" | "skip" | null>(null);

  // Against what is STORED, so an unset field's default can be pinned.
  const repostChanged = repostField.isChanged(repost);
  const skipChanged = skipField.isChanged(skip);

  /**
   * `update-setting` was a dead BFF path (#1057); this is the same write on
   * the command client. Both keys are already `settings_change` columns, so
   * the key name goes through unchanged and the port validates it.
   *
   * The two fields save INDEPENDENTLY, which is why each sends only its own
   * key. Sending both would make saving one silently rewrite the other with
   * whatever was in the box — a value the person never chose to submit.
   */
  async function save(key: "repost_ttl_days" | "skip_ttl_days", value: number) {
    onError(null);
    setSaving(key === "repost_ttl_days" ? "repost" : "skip");
    const result = await submitSettingsChange(workspaceId, { [key]: value });
    setSaving(null);

    if (!result.ok) {
      onError(settingsRefusalCopy(result.error, result.status));
      return;
    }
    router.refresh();
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">Repost Cadence</CardTitle>
      </CardHeader>
      <CardContent className="space-y-4">
        <p className="text-sm text-muted-foreground">
          How long a media item must wait before becoming eligible to post again,
          and how long a manual <span className="font-mono">Skip</span> defers an item.
        </p>
        <div className="grid gap-4 sm:grid-cols-2">
          <div className="space-y-2">
            <Label htmlFor="repost-ttl">Repost lock (days)</Label>
            <div className="flex gap-2">
              <Input
                id="repost-ttl"
                type="number"
                min={1}
                max={365}
                value={repost}
                onChange={(e) => setRepost(Number(e.target.value))}
                className="max-w-[120px]"
              />
              <Button
                onClick={() => save("repost_ttl_days", repost)}
                disabled={!repostChanged || saving !== null}
              >
                {saving === "repost" ? "Saving…" : "Save"}
              </Button>
            </div>
            {repostField.usingDefault && !repostChanged ? (
              <p className="text-xs text-muted-foreground">Using the default</p>
            ) : null}
          </div>
          <div className="space-y-2">
            <Label htmlFor="skip-ttl">Skip lock (days)</Label>
            <div className="flex gap-2">
              <Input
                id="skip-ttl"
                type="number"
                min={1}
                max={365}
                value={skip}
                onChange={(e) => setSkip(Number(e.target.value))}
                className="max-w-[120px]"
              />
              <Button
                onClick={() => save("skip_ttl_days", skip)}
                disabled={!skipChanged || saving !== null}
              >
                {saving === "skip" ? "Saving…" : "Save"}
              </Button>
            </div>
            {skipField.usingDefault && !skipChanged ? (
              <p className="text-xs text-muted-foreground">Using the default</p>
            ) : null}
          </div>
        </div>
      </CardContent>
    </Card>
  );
}
