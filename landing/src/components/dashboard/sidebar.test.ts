import { describe, expect, it } from "vitest";
import { activeHref } from "./sidebar";

/** One nav item lights up per page: the longest href the path sits under. */
describe("activeHref", () => {
  it.each([
    ["/dashboard", "/dashboard"],
    ["/dashboard/queue", "/dashboard/queue"],
    ["/dashboard/media", "/dashboard/media"],
    // The Calendar sits under the Media library's href; only Calendar lights.
    ["/dashboard/media/calendar", "/dashboard/media/calendar"],
    ["/dashboard/settings/members", "/dashboard/settings"],
  ])("%s lights %s", (pathname, href) => {
    expect(activeHref(pathname)).toBe(href);
  });

  it("lights nothing for a page outside the nav, and Overview only on its own page", () => {
    expect(activeHref("/dashboard/analytics")).toBeUndefined();
    expect(activeHref("/dashboard/queued")).toBeUndefined();
  });
});
