import { describe, expect, it } from "vitest";
import { dateInZone, formatCalendarDate, formatInZone, wallTimeInZone } from "./zoned-dates";

/**
 * 01:30 UTC on Oct 2 is 9:30 PM on Oct 1 in New York: the evening hour at
 * which a UTC server and a New York browser disagree about the day (#1511).
 * Microseconds, as Postgres renders them.
 */
const EVENING = "2026-10-02T01:30:00.123456+00:00";

/** ICU puts a narrow no-break space before AM/PM; compare words, not bytes. */
const spaced = (s: string) => s.replace(/\s/g, " ");

describe("dates on a named clock", () => {
  it("reads the day an instant falls on in the zone it is asked about", () => {
    expect(dateInZone(EVENING, "America/New_York")).toBe("2026-10-01");
    expect(dateInZone(EVENING, "UTC")).toBe("2026-10-02");
  });

  it("formats an instant on that zone's clock", () => {
    const shape = { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" } as const;
    expect(spaced(formatInZone(EVENING, "America/New_York", shape))).toBe("Oct 1, 9:30 PM");
    expect(spaced(formatInZone(EVENING, "UTC", shape))).toBe("Oct 2, 1:30 AM");
  });

  it("labels a YYYY-MM-DD as the day it names", () => {
    expect(formatCalendarDate("2026-09-03", { month: "short", day: "numeric" })).toBe("Sep 3");
    expect(formatCalendarDate("2026-10-01", { month: "long", year: "numeric" })).toBe("October 2026");
  });

  it("falls back to UTC for a zone it does not know, rather than throwing in a render", () => {
    expect(dateInZone(EVENING, "Not/AZone")).toBe("2026-10-02");
    // A shown time says which clock it fell back to, as formatSlot does.
    const shape = { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" } as const;
    expect(spaced(formatInZone(EVENING, "Not/AZone", shape))).toBe("Oct 2, 1:30 AM UTC");
  });

  it("gives an instant's wall time on a zone's clock, as a datetime-local input takes it", () => {
    // Reschedule… opens on the story's own time, in the story's own zone (#1413).
    expect(wallTimeInZone(EVENING, "America/New_York")).toBe("2026-10-01T21:30");
    expect(wallTimeInZone(EVENING, "UTC")).toBe("2026-10-02T01:30");
    // Midnight is 00, never the 24 an hour12:false clock can print.
    expect(wallTimeInZone("2026-10-02T04:00:00+00:00", "America/New_York")).toBe("2026-10-02T00:00");
  });
});
