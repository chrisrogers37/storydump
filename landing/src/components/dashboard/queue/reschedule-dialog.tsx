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
import { RepeatFields } from "@/components/dashboard/media/repeat-fields";
import type { RescheduleChange } from "@/lib/command-client";
import { formatSlot, type Intent } from "@/lib/intents";
import {
  NO_REPEAT,
  describeRepeat,
  formFromRule,
  ruleFromForm,
  sameRule,
  withDate,
  type RepeatForm,
} from "@/lib/repeat";
import { describedBy } from "@/lib/utils";
import { wallTimeInZone } from "@/lib/zoned-dates";

/** What the dialog's fields hold: a time, and a rule's form. */
export type RescheduleFields = { localAt: string; form: RepeatForm };

/**
 * What one press sends: the time when it moved, and the rule when it differs
 * from the one the dialog opened on (Never clears it). Nothing changed, nothing
 * is sent. An unchanged rule sent again would record a change that never
 * happened, and could overwrite one made elsewhere since the page read it.
 */
export function rescheduleChange(
  opened: RescheduleFields,
  now: RescheduleFields,
): RescheduleChange | null {
  const rule = ruleFromForm(now.form);
  const change: RescheduleChange = {
    ...(now.localAt !== opened.localAt ? { localAt: now.localAt } : {}),
    ...(sameRule(rule, ruleFromForm(opened.form)) ? {} : { repeat: rule }),
  };
  return Object.keys(change).length > 0 ? change : null;
}

/**
 * Reschedule…: move a planned story to another time (#1413). The same
 * story moves; its account and its item stay.
 *
 * The time is the STORY's. The dialog opens on the story's current time read
 * in its own zone (`intent.tz`, the port's `COALESCE(a.tz, w.tz)`), names that
 * zone, and sends what a person types as typed. Nothing converts between
 * zones, so the browser's own zone plays no part.
 *
 * It holds the form and nothing else, and reaches no network: `onSubmit` sends
 * the change and answers with the refusal's sentence, or null once it landed.
 *
 * Edit series… (#485, `series`) is the same dialog for a repeating story: it
 * also edits the rule, opened on the story's own, and Never stops it. A new
 * time moves the series with it, and a new date the day of a weekly series
 * that repeats on one (`withDate`), as the port moves it.
 */
export function RescheduleDialog({
  intent,
  label,
  disabled,
  series = false,
  onSubmit,
}: {
  intent: Intent;
  /** The row's button. */
  label: string;
  disabled: boolean;
  /** Edit series…: the rule is edited too. */
  series?: boolean;
  onSubmit: (intent: Intent, change: RescheduleChange) => Promise<string | null>;
}) {
  const ids = useId();
  const [open, setOpen] = useState(false);
  // What the dialog opened on, read once: a press sends only what differs.
  const [opened, setOpened] = useState<RescheduleFields>({ localAt: "", form: NO_REPEAT });
  const [localAt, setLocalAt] = useState("");
  const [repeat, setRepeat] = useState<RepeatForm>(NO_REPEAT);
  const [notice, setNotice] = useState<string | null>(null);
  const [pending, setPending] = useState(false);

  function onOpenChange(next: boolean) {
    if (pending) return;
    if (next) {
      const fields = {
        localAt: wallTimeInZone(intent.schedule_slot_at, intent.tz),
        form: formFromRule(intent.repeat),
      };
      setOpened(fields);
      setLocalAt(fields.localAt);
      setRepeat(fields.form);
      setNotice(null);
    }
    setOpen(next);
  }

  async function submit() {
    if (!localAt || pending) return;
    const change = rescheduleChange(opened, { localAt, form: repeat });
    if (change === null) {
      setOpen(false); // nothing changed, so nothing is sent
      return;
    }
    // An alert whose text does not change is not read again, so the same
    // refusal twice would pass in silence: clear it before asking.
    setNotice(null);
    setPending(true);
    try {
      const refusal = await onSubmit(intent, change);
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
          <DialogTitle className="break-words">
            {series ? `Edit the series of ${intent.file_name}` : `Reschedule ${intent.file_name}`}
          </DialogTitle>
          <DialogDescription>
            Set for {formatSlot(intent.schedule_slot_at, intent.tz)}.{" "}
            {series && intent.repeat
              ? `It repeats ${describeRepeat(intent.repeat)}. A new time moves the series with it, and its day too when it repeats on one.`
              : "At the new time it is sent for approval, as before."}
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
              onChange={(event) => {
                const next = event.target.value;
                if (series) setRepeat((form) => withDate(form, localAt, next));
                setLocalAt(next);
              }}
            />
            <p id={`${ids}-zone`} className="text-xs text-muted-foreground">
              In {intent.tz}, this story&apos;s time zone.
            </p>
          </div>
          {series && (
            <RepeatFields form={repeat} localAt={localAt} onChange={setRepeat} disabled={pending} />
          )}
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
              {series ? "Save" : "Reschedule"}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
