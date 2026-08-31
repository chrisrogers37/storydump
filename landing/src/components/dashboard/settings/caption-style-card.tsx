"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { settingsRefusalCopy, submitSettingsChange } from "@/lib/command-client";

interface Props {
  /** The gate this screen is held behind; not this card's to decide. */
  editable: boolean;
  workspaceId: string;
  captionStyle: string | null;
  onError: (message: string | null) => void;
}

const STYLE_OPTIONS = [
  {
    value: "enhanced",
    label: "Enhanced",
    description: "Emoji headers, separators, and a bold layout.",
  },
  {
    value: "simple",
    label: "Simple",
    description: "Plain-text caption with no extra formatting.",
  },
];

export function CaptionStyleCard({ captionStyle, workspaceId, editable, onError }: Props) {
  const router = useRouter();
  const initial = captionStyle ?? "enhanced";
  const [value, setValue] = useState(initial);
  const [saving, setSaving] = useState(false);

  const changed = value !== initial;

  /**
   * `update-string-setting` was one of the 24 BFF paths that resolved to no
   * route (#1057). It is now a `settings_change` on the one command client,
   * which carries the `Idempotency-Key` the port requires.
   *
   * `caption_style` is sent as the key the PORT names. The old path took a
   * `setting_name` string and a value; the allowlist lives server-side and
   * refuses an unknown key by name, so nothing here re-states it.
   */
  async function save() {
    onError(null);
    setSaving(true);
    const result = await submitSettingsChange(workspaceId, { caption_style: value });
    setSaving(false);

    if (!result.ok) {
      onError(settingsRefusalCopy(result.error, result.status));
      return;
    }
    // Re-read rather than trust the submitted value: `initial` comes from the
    // server, and it is what closes the button again.
    router.refresh();
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">Caption Style</CardTitle>
      </CardHeader>
      <CardContent className="space-y-4">
        <div className="space-y-2">
          <Label>Telegram notification format</Label>
          <Select value={value} onValueChange={setValue} disabled={!editable}>
            <SelectTrigger className="w-full sm:w-[260px]">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {STYLE_OPTIONS.map((opt) => (
                <SelectItem key={opt.value} value={opt.value}>
                  {opt.label}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
          <p className="text-sm text-muted-foreground">
            {STYLE_OPTIONS.find((o) => o.value === value)?.description}
          </p>
        </div>
        {editable && (
        <Button onClick={save} disabled={!changed || saving}>
          {saving ? "Saving…" : "Save"}
        </Button>
        )}
      </CardContent>
    </Card>
  );
}
