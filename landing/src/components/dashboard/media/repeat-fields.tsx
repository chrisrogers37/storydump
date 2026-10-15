"use client";

import { useId } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import {
  WEEKDAYS,
  dayName,
  offDayWarning,
  repeatSentence,
  withUnit,
  type RepeatForm,
} from "@/lib/repeat";

/**
 * Repeat, as the Schedule and Edit series dialogs ask it (#485): Never, Every
 * day or Every week; every N of them; for a weekly rule, its weekdays, which
 * start on the chosen date's; and the rule as a sentence, or a warning when
 * its days leave the date out. It holds no state: the dialog owns the form and
 * the time it repeats from, and moves a weekly form's day with the date
 * (`withDate`). Nothing here bounds the rule, since the port judges it and a
 * refusal says so.
 */
export function RepeatFields({
  form,
  localAt,
  onChange,
  disabled = false,
}: {
  form: RepeatForm;
  /** The chosen time, the first occurrence: the sentence starts from it. */
  localAt: string;
  onChange: (form: RepeatForm) => void;
  disabled?: boolean;
}) {
  const ids = useId();
  const sentence = repeatSentence(localAt, form);
  const warning = offDayWarning(localAt, form);

  return (
    <div className="space-y-2">
      <Label htmlFor={`${ids}-repeat`}>Repeat</Label>
      <Select
        value={form.unit}
        disabled={disabled}
        onValueChange={(unit) => onChange(withUnit(form, unit as RepeatForm["unit"], localAt))}
      >
        <SelectTrigger id={`${ids}-repeat`} className="w-full">
          <SelectValue />
        </SelectTrigger>
        <SelectContent>
          <SelectItem value="never">Never</SelectItem>
          <SelectItem value="day">Every day</SelectItem>
          <SelectItem value="week">Every week</SelectItem>
        </SelectContent>
      </Select>
      {form.unit !== "never" && (
        <div className="flex items-center gap-2">
          <Label htmlFor={`${ids}-every`} className="shrink-0">
            Every
          </Label>
          <Input
            id={`${ids}-every`}
            type="number"
            inputMode="numeric"
            min={1}
            className="w-20"
            disabled={disabled}
            value={form.every}
            onChange={(event) => onChange({ ...form, every: Number(event.target.value) })}
          />
          <span className="text-sm">{form.every === 1 ? form.unit : `${form.unit}s`}</span>
        </div>
      )}
      {form.unit === "week" && (
        <fieldset className="flex flex-wrap gap-1">
          <legend className="sr-only">On</legend>
          {WEEKDAYS.map((_, index) => {
            const day = index + 1;
            const on = form.days.includes(day);
            return (
              <Button
                key={day}
                type="button"
                size="xs"
                variant={on ? "default" : "outline"}
                aria-pressed={on}
                disabled={disabled}
                onClick={() =>
                  onChange({
                    ...form,
                    days: on ? form.days.filter((d) => d !== day) : [...form.days, day],
                  })
                }
              >
                {dayName(day)}
              </Button>
            );
          })}
        </fieldset>
      )}
      {warning ? (
        <p className="text-xs text-destructive">{warning}</p>
      ) : (
        sentence && <p className="text-xs text-muted-foreground">{sentence}</p>
      )}
    </div>
  );
}
