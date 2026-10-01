import { describe, expect, it } from "vitest";
import { deriveRunway, runwayHeadline, type RunwayAccount } from "./runway";

function account(over: Partial<RunwayAccount> = {}): RunwayAccount {
  return {
    id: "a1",
    handle: "storyco",
    display_name: null,
    state: "active",
    posting: true,
    posts_per_day: 3,
    eligible: 20,
    // Whole days of 20 files at 3 a day, counted down on the server.
    days_left: 6,
    low: true,
    ...over,
  };
}

describe("runwayHeadline", () => {
  it("shows the server's whole days unchanged", () => {
    expect(runwayHeadline(6)).toBe("About 6 days");
    expect(runwayHeadline(7)).toBe("About 7 days");
    expect(runwayHeadline(1)).toBe("About 1 day");
  });

  it("reads no whole day left as less than a day, never as zero days", () => {
    expect(runwayHeadline(0)).toBe("Less than a day");
  });

  it("says an account that is not posting has no runway, not zero days", () => {
    expect(runwayHeadline(null)).toBe("Not posting");
  });
});

describe("deriveRunway", () => {
  it("names the account as every surface does and shows the arithmetic", () => {
    const [row] = deriveRunway({ below_days: 7, accounts: [account()] });
    expect(row).toEqual({
      key: "a1",
      name: "storyco",
      headline: "About 6 days",
      detail: "20 files at 3 a day",
      low: true,
    });
  });

  it("prefers the name Instagram gave the account", () => {
    const [row] = deriveRunway({
      below_days: 7,
      accounts: [account({ display_name: "Story Co" })],
    });
    expect(row.name).toBe("Story Co");
  });

  it("does not divide for an account the clock does not post for", () => {
    const [row] = deriveRunway({
      below_days: 7,
      accounts: [account({ posting: false, days_left: null, low: false, eligible: 1 })],
    });
    expect(row.headline).toBe("Not posting");
    expect(row.detail).toBe("1 file ready");
    expect(row.low).toBe(false);
  });

  it("takes the low mark from the server rather than recounting it", () => {
    const [row] = deriveRunway({
      below_days: 7,
      accounts: [account({ eligible: 30, days_left: 10, low: true })],
    });
    expect(row.low).toBe(true);
  });

  it("keeps the server's order and one row per account", () => {
    const rows = deriveRunway({
      below_days: 7,
      accounts: [account({ id: "a1" }), account({ id: "a2", handle: "second" })],
    });
    expect(rows.map((row) => row.key)).toEqual(["a1", "a2"]);
  });
});
