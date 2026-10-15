import { describe, expect, it } from "vitest";
import { readFileSync } from "fs";
import { fileURLToPath } from "url";
import path from "path";
import {
  NO_REPEAT,
  describeRepeat,
  formFromRule,
  isRepeatRule,
  isoWeekday,
  offDayWarning,
  repeatPeriod,
  repeatSentence,
  ruleFromForm,
  sameRule,
  withDate,
  withUnit,
  type RepeatRule,
} from "./repeat";

/**
 * A repeat rule in the browser (#485): its words, and the forms' mapping to
 * and from it. Nothing here computes a date the ledger makes; the one date
 * read is the weekday of a date the person typed.
 */

const rule = (over: Partial<RepeatRule> = {}): RepeatRule => ({
  unit: "week",
  every: 1,
  days: [1, 4],
  at: "16:07",
  ...over,
});

/**
 * The words for a rule's period, pinned once in `tests/fixtures/repeat_words.json`
 * and read here and by the vocabulary's own test (`describe_repeat`), so the web
 * and the CLI and card cannot drift apart.
 */
const WORDS = path.resolve(
  path.dirname(fileURLToPath(import.meta.url)),
  "../../../tests/fixtures/repeat_words.json",
);
const WORD_CASES = (
  JSON.parse(readFileSync(WORDS, "utf8")) as {
    cases: { rule: Omit<RepeatRule, "at">; period: string }[];
  }
).cases;

describe("repeatPeriod — the CLI's and the card's words", () => {
  it.each(WORD_CASES.map((c) => [c.period, c.rule] as const))("%s", (period, given) => {
    expect(repeatPeriod(given)).toBe(period);
  });
});

describe("describeRepeat — the period, on the web's clock face", () => {
  it.each([
    [rule({ unit: "day", days: null, at: "09:00" }), "every day at 9:00 AM"],
    [rule(), "every week on Mon and Thu at 4:07 PM"],
  ])("%j", (given, words) => {
    expect(describeRepeat(given)).toBe(words);
  });
});

describe("the forms' mapping", () => {
  it("sends nothing for Never, and the unit with what was chosen otherwise", () => {
    expect(ruleFromForm(NO_REPEAT)).toBeNull();
    expect(ruleFromForm({ unit: "day", every: 2, days: [] })).toEqual({ unit: "day", every: 2 });
    expect(ruleFromForm({ unit: "week", every: 1, days: [4, 1] })).toEqual({
      unit: "week",
      every: 1,
      days: [4, 1],
    });
  });

  it("sends no days for a weekly rule with none chosen, so the port's default applies", () => {
    expect(ruleFromForm({ unit: "week", every: 2, days: [] })).toEqual({ unit: "week", every: 2 });
  });

  it("knows a rule unchanged however its days were pressed", () => {
    const weekly = { unit: "week" as const, every: 1, days: [1, 4] };
    expect(sameRule(weekly, { ...weekly, days: [4, 1] })).toBe(true);
    expect(sameRule(weekly, { ...weekly, every: 2 })).toBe(false);
    expect(sameRule(weekly, { ...weekly, days: [1] })).toBe(false);
    expect(sameRule(weekly, null)).toBe(false);
    expect(sameRule(null, null)).toBe(true);
  });

  it("opens Edit series on the story's own rule, and Never for none", () => {
    expect(formFromRule(null)).toEqual(NO_REPEAT);
    expect(formFromRule(rule())).toEqual({ unit: "week", every: 1, days: [1, 4] });
    expect(ruleFromForm(formFromRule(rule()))).toEqual({ unit: "week", every: 1, days: [1, 4] });
  });

  it("starts a weekly rule on the chosen date's weekday, as the port would", () => {
    // 2026-10-15 is a Thursday.
    expect(isoWeekday("2026-10-15")).toBe(4);
    expect(isoWeekday("2026-10-18")).toBe(7);
    expect(withUnit(NO_REPEAT, "week", "2026-10-15T16:07").days).toEqual([4]);
    // Chosen days are kept; a daily rule takes none; no time yet, no default.
    expect(withUnit({ unit: "day", every: 1, days: [2] }, "week", "2026-10-15T16:07").days).toEqual([2]);
    expect(withUnit(NO_REPEAT, "day", "2026-10-15T16:07").days).toEqual([]);
    expect(withUnit(NO_REPEAT, "week", "").days).toEqual([]);
  });
});

describe("a weekly form and its date, as the port keeps a series to its days", () => {
  const weekly = (days: number[]) => ({ unit: "week" as const, every: 1, days });

  it("moves a series of one day with the date, as the port moves it", () => {
    // Thursday, Oct 15 to Wednesday, Oct 14.
    expect(withDate(weekly([4]), "2026-10-15T16:07", "2026-10-14T16:07").days).toEqual([3]);
  });

  it("keeps days a person chose, and anything that is not a weekly rule", () => {
    expect(withDate(weekly([5]), "2026-10-15T16:07", "2026-10-14T16:07").days).toEqual([5]);
    expect(withDate(weekly([1, 4]), "2026-10-15T16:07", "2026-10-14T16:07").days).toEqual([1, 4]);
    const daily = { unit: "day" as const, every: 1, days: [] };
    expect(withDate(daily, "2026-10-15T16:07", "2026-10-14T16:07")).toBe(daily);
    expect(withDate(weekly([4]), "", "2026-10-14T16:07").days).toEqual([4]);
    expect(withDate(weekly([4]), "2026-10-15T16:07", "").days).toEqual([4]);
  });

  it("warns when the days leave the date out, which the port refuses", () => {
    expect(offDayWarning("2026-10-14T16:07", weekly([1, 4]))).toBe(
      "Wed is not one of the days it repeats on. Add Wed, or pick a date on one of them.",
    );
    expect(offDayWarning("2026-10-15T16:07", weekly([1, 4]))).toBeNull();
    // No day chosen yet takes the date's; Never and daily rules have no days.
    expect(offDayWarning("2026-10-14T16:07", weekly([]))).toBeNull();
    expect(offDayWarning("2026-10-14T16:07", { unit: "day", every: 1, days: [4] })).toBeNull();
    expect(offDayWarning("", weekly([1, 4]))).toBeNull();
  });
});

describe("repeatSentence — what the form says it will do", () => {
  it("names the first occurrence and the rule", () => {
    expect(repeatSentence("2026-10-15T16:07", { unit: "week", every: 1, days: [1, 4] })).toBe(
      "From Thu, Oct 15 at 4:07 PM, every week on Mon and Thu",
    );
    expect(repeatSentence("2026-10-15T09:00", { unit: "day", every: 2, days: [] })).toBe(
      "From Thu, Oct 15 at 9:00 AM, every 2 days",
    );
  });

  it("says the chosen date's weekday for a weekly rule with none chosen, as the port will", () => {
    expect(repeatSentence("2026-10-15T16:07", { unit: "week", every: 1, days: [] })).toBe(
      "From Thu, Oct 15 at 4:07 PM, every week on Thu",
    );
  });

  it("says nothing for Never, or before a time is chosen", () => {
    expect(repeatSentence("2026-10-15T16:07", NO_REPEAT)).toBeNull();
    expect(repeatSentence("", { unit: "day", every: 1, days: [] })).toBeNull();
  });
});

describe("isRepeatRule — an answer's rule, checked before it is said", () => {
  it("admits the one shape and nothing else", () => {
    expect(isRepeatRule(rule())).toBe(true);
    expect(isRepeatRule(rule({ unit: "day", days: null }))).toBe(true);
    expect(isRepeatRule(null)).toBe(false);
    expect(isRepeatRule({ ...rule(), unit: "month" })).toBe(false);
    expect(isRepeatRule({ ...rule(), at: undefined })).toBe(false);
  });
});
