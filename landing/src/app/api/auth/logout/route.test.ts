/**
 * Sign-out is POST-only, and that is not a style question: a GET export
 * would revoke a session whenever anything fetched the URL. Next prefetches a
 * `next/link` on render, which issues `GET /api/auth/logout?_rsc=…` with
 * nobody clicking.
 *
 * The narrow reading is "do not prefetch that link". The real one is that a GET
 * must not mutate state, because prefetch is only one of the things that fetch
 * URLs speculatively — crawlers, link previewers, chat unfurlers and email
 * clients all do, and none of them will be in the next reviewer's head.
 *
 * So the assertion is on the MODULE'S EXPORTS, not on the caller. A future
 * `<Link>` to this route is then a rendering choice rather than an outage.
 */

import { describe, expect, it } from "vitest";
import * as logoutRoute from "./route";

describe("the sign-out route's HTTP surface", () => {
  it("does not export GET — a prefetch must not be able to revoke a session", () => {
    expect("GET" in logoutRoute).toBe(false);
  });

  it("exports POST, so sign-out still works", () => {
    expect(typeof (logoutRoute as Record<string, unknown>).POST).toBe("function");
  });

  it("exports no other state-mutating verb by accident", () => {
    const verbs = ["GET", "HEAD", "PUT", "PATCH", "DELETE", "OPTIONS"];
    expect(verbs.filter((v) => v in logoutRoute)).toEqual([]);
  });
});
