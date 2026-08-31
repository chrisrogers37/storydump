/**
 * Sign-out is POST-only, and this file exists because the GET export was a
 * live production outage rather than a style question.
 *
 * `/welcome` linked here with `next/link`. Next prefetched the link on render,
 * which issued `GET /api/auth/logout?_rsc=…` with nobody clicking, and the GET
 * export revoked the session about a second after it was minted — observed in
 * the request stream, and matched from the database side by sessions revoked
 * 0.5-5.9s after mint, 7 of 7, with `expires_at` 30 days out.
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
