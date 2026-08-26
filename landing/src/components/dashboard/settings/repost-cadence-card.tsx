"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { settingsRefusalCopy, submitSettingsChange } from "@/lib/command-client";

interface Props {
  /** The gate this screen is held behind; not this card's to decide. */
  editable: boolean;
  workspaceId: string;
  repostTtlDays: number | null;
  skipTtlDays: number | null;
  onError: (message: string | null) => void;
}

/** Per-chat lock TTLs. Null in DB = use deployment env defaults (`REPOST_TTL_DAYS`,
 * `SKIP_TTL_DAYS`). The chat_settings row is bootstrapped with those env values,
 * so values arrive here populated for any chat created after migration 029.
 */
export function RepostCadenceCard({
  repostTtlDays,
  skipTtlDays,
  workspaceId,
  editable,
  onError,
}: Props) {
  const router = useRouter();
  const [repost, setRepost] = useState<number>(repostTtlDays ?? 30);
  const [skip, setSkip] = useState<number>(skipTtlDays ?? 45);
  const [saving, setSaving] = useState<"repost" | "skip" | null>(null);

  const repostChanged = repost !== (repostTtlDays ?? 30);
  const skipChanged = skip !== (skipTtlDays ?? 45);

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
      onError(settingsRefusalCopy(result.error));
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
                disabled={!editable}
                id="repost-ttl"
                type="number"
                min={1}
                max={365}
                value={repost}
                onChange={(e) => setRepost(Number(e.target.value))}
                className="max-w-[120px]"
              />
              {editable && <Button
                onClick={() => save("repost_ttl_days", repost)}
                disabled={!repostChanged || saving !== null}
              >
                {saving === "repost" ? "Saving…" : "Save"}
              </Button>}
            </div>
          </div>
          <div className="space-y-2">
            <Label htmlFor="skip-ttl">Skip lock (days)</Label>
            <div className="flex gap-2">
              <Input
                disabled={!editable}
                id="skip-ttl"
                type="number"
                min={1}
                max={365}
                value={skip}
                onChange={(e) => setSkip(Number(e.target.value))}
                className="max-w-[120px]"
              />
              {editable && <Button
                onClick={() => save("skip_ttl_days", skip)}
                disabled={!skipChanged || saving !== null}
              >
                {saving === "skip" ? "Saving…" : "Save"}
              </Button>}
            </div>
          </div>
        </div>
      </CardContent>
    </Card>
  );
}
