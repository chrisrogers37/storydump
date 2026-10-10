import { describe, expect, it } from "vitest";
import {
  addDays,
  monthGrid,
  monthOf,
  monthParam,
  parseDay,
  parseMonth,
  shiftMonth,
} from "./calendar-month";

describe("the calendar's month and day params (#1634)", () => {
  it("reads a month, and nothing that is not one", () => {
    expect(parseMonth("2026-10")).toEqual({ year: 2026, month: 10 });
    for (const bad of ["2026-13", "2026-00", "2026-1", "26-10", "2026-10-01", "", undefined, 202610]) {
      expect(parseMonth(bad)).toBeNull();
    }
  });

  it("reads only a year it can draw: four digits, its grid inside year 9999", () => {
    // 0999 would be named "999" and fail to format; 9999's grid runs into 10000.
    for (const bad of ["0999-12", "0050-01", "9999-12"]) {
      expect(parseMonth(bad)).toBeNull();
    }
    expect(parseMonth("1000-01")).toEqual({ year: 1000, month: 1 });
    expect(parseDay("0999-12-01")).toBeNull();
    expect(parseDay("9999-12-31")).toBeNull();
  });

  it("reads a day only when it is a real one", () => {
    expect(parseDay("2026-10-03")).toBe("2026-10-03");
    expect(parseDay("2028-02-29")).toBe("2028-02-29");
    for (const bad of ["2026-02-29", "2026-10-32", "2026-10-3", "2026-10", "", undefined]) {
      expect(parseDay(bad)).toBeNull();
    }
  });

  it("names a month, steps it across a year and finds a day's", () => {
    expect(monthParam({ year: 2026, month: 3 })).toBe("2026-03");
    expect(shiftMonth({ year: 2026, month: 12 }, 1)).toEqual({ year: 2027, month: 1 });
    expect(shiftMonth({ year: 2026, month: 1 }, -1)).toEqual({ year: 2025, month: 12 });
    expect(monthOf("2026-10-03")).toEqual({ year: 2026, month: 10 });
  });

  it("adds days across a month and a year", () => {
    expect(addDays("2026-10-31", 1)).toBe("2026-11-01");
    expect(addDays("2026-12-31", 1)).toBe("2027-01-01");
    expect(addDays("2026-03-01", -1)).toBe("2026-02-28");
  });
});

describe("the month's grid", () => {
  it("is whole Monday-first weeks, and the half-open range the page asks for", () => {
    // Oct 1, 2026 is a Thursday and Oct 31 a Saturday.
    const grid = monthGrid({ year: 2026, month: 10 });
    expect(grid.dates[0]).toBe("2026-09-28");
    expect(grid.dates.at(-1)).toBe("2026-11-01");
    expect(grid.dates).toHaveLength(35);
    expect([grid.from, grid.to]).toEqual(["2026-09-28", "2026-11-02"]);
  });

  it("never asks for more than six weeks, the API's range", () => {
    for (let month = 1; month <= 12; month++) {
      expect(monthGrid({ year: 2026, month }).dates.length).toBeLessThanOrEqual(42);
    }
  });
});
