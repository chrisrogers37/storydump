import { joinWithAnd } from "./utils";
import { formatCalendarDate, formatInZone } from "./zoned-dates";

/**
 * A planned story's repeat rule (#485): its one shape, its words, and the
 * Schedule and Edit series forms' mapping to and from it.
 *
 * The rule is the API's, `{unit, every, days, at}`
 * (`intent_ledger.repeat_sql`): every `every` days, or every `every` weeks on
 * the ISO weekdays `days` (1 is Monday), at the local time `at` (`HH:MM`) in
 * the story's zone. The browser computes no date from it. The ledger makes
 * each next occurrence (`fn_repeat_times`), and the port fills a rule's
 * defaults and judges its bounds, so a date or a refusal shown here is one the
 * API gave.
 */

/** The units a rule repeats by — the vocabulary's `REPEAT_UNITS` (`wire-contract.test.ts` pins them equal). */
export const REPEAT_UNITS = ["day", "week"] as const;
export type RepeatUnit = (typeof REPEAT_UNITS)[number];

/**
 * The weekdays a weekly rule names, in ISO order — the vocabulary's
 * `WEEKDAYS`. A name's index plus one is its ISO weekday.
 */
export const WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"] as const;

/** A story's rule as the API answers it (`Intent.repeat`). */
export type RepeatRule = {
  unit: RepeatUnit;
  every: number;
  /** ISO weekdays, for a weekly rule; null for a daily one. */
  days: number[] | null;
  /** The local time of day, `HH:MM`, in the story's zone. */
  at: string;
};

/**
 * What a form sends as `repeat`: the parts a person chose. The port fills the
 * rest (every 1, and a weekly rule's day from the chosen date) and its CHECKs
 * judge the whole, so nothing here is bounded.
 */
export type RepeatRequest = { unit: RepeatUnit; every?: number; days?: number[] };

/** A form's state: Never, or a unit with its interval and weekdays. */
export type RepeatForm = { unit: RepeatUnit | "never"; every: number; days: number[] };

export const NO_REPEAT: RepeatForm = { unit: "never", every: 1, days: [] };

/** Weekdays each once and in order, however they were pressed. */
function inOrder(days: readonly number[]): number[] {
  return [...new Set(days)].sort((a, b) => a - b);
}

/** `Mon`, for ISO weekday 1: the sentence's and the toggles' one spelling. */
export function dayName(iso: number): string {
  const name = WEEKDAYS[iso - 1] ?? "?";
  return name.charAt(0).toUpperCase() + name.slice(1);
}

/**
 * The rule's period and days without its time: "every week on Mon and Thu".
 * The CLI's and the card's words (`vocabulary.describe_repeat`), which
 * `tests/fixtures/repeat_words.json` holds the two to.
 */
export function repeatPeriod(rule: Pick<RepeatRule, "unit" | "every" | "days">): string {
  if (rule.unit === "day") {
    return rule.every === 1 ? "every day" : `every ${rule.every} days`;
  }
  const period = rule.every === 1 ? "every week" : `every ${rule.every} weeks`;
  const names = inOrder(rule.days ?? []).map(dayName);
  return names.length === 0 ? period : `${period} on ${joinWithAnd(names)}`;
}

/** A wall time of day, `HH:MM`, on the app's clock face: "4:07 PM". */
function clockFace(hhmm: string): string {
  return formatInZone(`1970-01-01T${hhmm}:00Z`, "UTC", { hour: "numeric", minute: "2-digit" });
}

/**
 * A rule in the web's words: the CLI's and the card's sentence
 * (`vocabulary.describe_repeat`) with the time on the clock face every other
 * time here reads on, "every 2 weeks on Tue at 4:07 PM".
 */
export function describeRepeat(rule: RepeatRule): string {
  return `${repeatPeriod(rule)} at ${clockFace(rule.at)}`;
}

/** A typed time that starts with its date, `YYYY-MM-DD`. */
const DATED = /^\d{4}-\d{2}-\d{2}/;

/** The ISO weekday of a typed `YYYY-MM-DD` date: no zone, since the date is the person's own. */
export function isoWeekday(localDate: string): number {
  const day = new Date(`${localDate}T00:00:00Z`).getUTCDay();
  return day === 0 ? 7 : day;
}

/** Whether *value* is a rule in its one shape, as an answer carries it. */
export function isRepeatRule(value: unknown): value is RepeatRule {
  if (typeof value !== "object" || value === null) return false;
  const rule = value as Record<string, unknown>;
  return (
    (REPEAT_UNITS as readonly unknown[]).includes(rule.unit) &&
    typeof rule.every === "number" &&
    typeof rule.at === "string" &&
    (rule.days === null || Array.isArray(rule.days))
  );
}

/**
 * What a form sends: nothing for Never, else the unit and what was chosen. A
 * weekly rule with no day chosen sends none, and the port puts it on the
 * chosen date's weekday.
 */
export function ruleFromForm(form: RepeatForm): RepeatRequest | null {
  if (form.unit === "never") return null;
  if (form.unit === "day") return { unit: "day", every: form.every };
  return { unit: "week", every: form.every, ...(form.days.length > 0 ? { days: form.days } : {}) };
}

/**
 * Whether two requests ask for the same rule: the same unit, interval and
 * days, in whatever order the days were pressed. No rule is the same only as
 * no rule.
 */
export function sameRule(a: RepeatRequest | null, b: RepeatRequest | null): boolean {
  if (a === null || b === null) return a === b;
  return (
    a.unit === b.unit &&
    (a.every ?? 1) === (b.every ?? 1) &&
    inOrder(a.days ?? []).join() === inOrder(b.days ?? []).join()
  );
}

/** The form for a story's rule, Never for none: the Edit series dialog opens on it. */
export function formFromRule(rule: RepeatRule | null): RepeatForm {
  if (!rule) return NO_REPEAT;
  return { unit: rule.unit, every: rule.every, days: rule.days ? [...rule.days] : [] };
}

/**
 * The form's unit changed. Every week starts on the chosen date's weekday,
 * which is where the port would put it too, so the toggles show what will be
 * sent.
 */
export function withUnit(
  form: RepeatForm,
  unit: RepeatForm["unit"],
  localAt: string,
): RepeatForm {
  if (unit === "week" && form.days.length === 0 && localAt) {
    return { ...form, unit, days: [isoWeekday(localAt.slice(0, 10))] };
  }
  return { ...form, unit };
}

/**
 * The chosen date moved from *previous* to *next*. A weekly form whose one
 * day was the old date's weekday moves to the new date's, as the port moves a
 * series of one day with its story (`_on_its_day`). Days a person chose stay
 * as chosen, and `offDayWarning` says when they leave the date out.
 */
export function withDate(form: RepeatForm, previous: string, next: string): RepeatForm {
  if (form.unit !== "week" || form.days.length !== 1) return form;
  if (!DATED.test(previous) || !DATED.test(next)) return form;
  return form.days[0] === isoWeekday(previous.slice(0, 10))
    ? { ...form, days: [isoWeekday(next.slice(0, 10))] }
    : form;
}

/**
 * What to say when a weekly form's days leave out the chosen date's weekday:
 * the series would post on that date and on its days too, so the port refuses
 * it (`off_day`). Null when the date is one of them, or there is nothing to
 * judge yet.
 */
export function offDayWarning(localAt: string, form: RepeatForm): string | null {
  if (form.unit !== "week" || form.days.length === 0 || !DATED.test(localAt)) return null;
  const day = isoWeekday(localAt.slice(0, 10));
  if (form.days.includes(day)) return null;
  const name = dayName(day);
  return `${name} is not one of the days it repeats on. Add ${name}, or pick a date on one of them.`;
}

/**
 * The sentence under the form: when the first occurrence is, and how it
 * repeats. "From Thu, Oct 15 at 4:07 PM, every week on Mon and Thu". A weekly
 * rule with no day chosen says the date's own, as the port will put it. Null
 * for Never, or before a time is chosen.
 */
export function repeatSentence(localAt: string, form: RepeatForm): string | null {
  if (form.unit === "never" || !/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}/.test(localAt)) return null;
  const date = localAt.slice(0, 10);
  const days = form.days.length > 0 ? form.days : [isoWeekday(date)];
  const on = formatCalendarDate(date, { weekday: "short", day: "numeric", month: "short" });
  return `From ${on} at ${clockFace(localAt.slice(11, 16))}, ${repeatPeriod({
    unit: form.unit,
    every: form.every,
    days,
  })}`;
}
