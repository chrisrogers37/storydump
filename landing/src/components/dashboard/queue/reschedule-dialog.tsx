"use client";

import { useId, useState } from "react";
import { Loader2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogClose,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { formatSlot, type Intent } from "@/lib/intents";
import { describedBy } from "@/lib/utils";
import { wallTimeInZone } from "@/lib/zoned-dates";

/**
 * Reschedule…: move a planned story to another time (#1413 phase 6). The same
 * story moves; its account and its item stay.
 *
 * The time is the STORY's. The dialog opens on the story's current time read
 * in its own zone (`intent.tz`, the port's `COALESCE(a.tz, w.tz)`), names that
 * zone, and sends what a person types as typed. Nothing converts between
 * zones, so the browser's own zone plays no part.
 *
 * It holds the form and nothing else, and reaches no network: `onSubmit` sends
 * the move and answers with the refusal's sentence, or null once it moved.
 */
export function RescheduleDialog({
  intent,
  label,
  disabled,
  onSubmit,
}: {
  intent: Intent;
  /** The row's button. */
  label: string;
  disabled: boolean;
  onSubmit: (intent: Intent, localAt: string) => Promise<string | null>;
}) {
  const ids = useId();
  const [open, setOpen] = useState(false);
  const [localAt, setLocalAt] = useState("");
  const [notice, setNotice] = useState<string | null>(null);
  const [pending, setPending] = useState(false);

  function onOpenChange(next: boolean) {
    if (pending) return;
    if (next) {
      setLocalAt(wallTimeInZone(intent.schedule_slot_at, intent.tz));
      setNotice(null);
    }
    setOpen(next);
  }

  async function submit() {
    if (!localAt || pending) return;
    // An alert whose text does not change is not read again, so the same
    // refusal twice would pass in silence: clear it before asking.
    setNotice(null);
    setPending(true);
    try {
      const refusal = await onSubmit(intent, localAt);
      if (refusal === null) setOpen(false);
      else setNotice(refusal);
    } finally {
      setPending(false);
    }
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogTrigger asChild>
        <Button size="sm" variant="outline" disabled={disabled}>
          {label}
        </Button>
      </DialogTrigger>
      <DialogContent>
        <DialogHeader>
          <DialogTitle className="break-words">Reschedule {intent.file_name}</DialogTitle>
          <DialogDescription>
            Set for {formatSlot(intent.schedule_slot_at, intent.tz)}. At the new time it is
            sent for approval, as before.
          </DialogDescription>
        </DialogHeader>
        <form
          className="space-y-4"
          onSubmit={(event) => {
            event.preventDefault();
            void submit();
          }}
        >
          <div className="space-y-2">
            <Label htmlFor={`${ids}-when`}>When</Label>
            <Input
              id={`${ids}-when`}
              type="datetime-local"
              required
              aria-describedby={describedBy(`${ids}-zone`, notice && `${ids}-notice`)}
              value={localAt}
              onChange={(event) => setLocalAt(event.target.value)}
            />
            <p id={`${ids}-zone`} className="text-xs text-muted-foreground">
              In {intent.tz}, this story&apos;s time zone.
            </p>
          </div>
          {notice && (
            <p id={`${ids}-notice`} role="alert" className="text-sm text-destructive">
              {notice}
            </p>
          )}
          <DialogFooter>
            <DialogClose asChild>
              <Button type="button" variant="outline" disabled={pending}>
                Keep it
              </Button>
            </DialogClose>
            <Button type="submit" disabled={pending || !localAt}>
              {pending ? <Loader2 className="h-4 w-4 animate-spin" aria-hidden /> : null}
              Reschedule
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
