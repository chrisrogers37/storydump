/**
 * The owner area's waitlist (#1723): the read's rows, each row's one action,
 * the outcome an admission's answer names, and the line every answer leaves.
 *
 * A read the page cannot trust is null, never `[]`: an empty list would tell
 * the owner no one is waiting. The states are the API's, read from its source
 * as `claim/[token]/page.test.tsx` reads the token's shape.
 * AN UNREADABLE SOURCE IS A FAILURE, NEVER A SKIP.
 */

import { readFileSync } from "fs";
import path from "path";
import { fileURLToPath } from "url";
import { describe, expect, it } from "vitest";
import type { BffResult } from "./bff";
import {
  ADMIT_OUTCOMES,
  admissionLine,
  admissionOutcome,
  joinedLabel,
  LOST_ANSWER,
  rowAction,
  STATE_LABELS,
  WAITLIST_COPY,
  WAITLIST_STATES,
  waitingLine,
  waitlistRows,
  type WaitlistRow,
} from "./ops-waitlist";

/** A row as `GET /api/v1/ops/waitlist` sends it. */
function wire(over: Record<string, unknown> = {}) {
  return {
    email: "person@example.com",
    on_waitlist: true,
    joined_at: "2026-10-12T09:30:00.123456+00:00",
    state: "waiting",
    admitted_at: null,
    emailed_at: null,
    ...over,
  };
}

const envelope = (rows: unknown) => ({ v: 1, kind: "waitlist", data: { rows }, error: null });

describe("the waitlist read", () => {
  it("reads each row's address, the day it joined and where it stands", () => {
    const rows = waitlistRows(
      envelope([wire(), wire({ email: "off@example.com", on_waitlist: false, joined_at: null, state: "admitted" })]),
    );
    expect(rows).toEqual([
      { email: "person@example.com", joinedAt: "2026-10-12T09:30:00.123456+00:00", state: "waiting" },
      { email: "off@example.com", joinedAt: null, state: "admitted" },
    ]);
  });

  it("is an empty list only when the API answered one", () => {
    expect(waitlistRows(envelope([]))).toEqual([]);
  });

  it.each([
    ["no envelope", null],
    ["no rows", { v: 1, kind: "waitlist", data: {}, error: null }],
    ["rows that are not a list", envelope({})],
    ["a row that is not an object", envelope([wire(), 7])],
    ["a row with no address", envelope([wire({ email: null })])],
    ["a state the page does not know", envelope([wire({ state: "declined" })])],
    ["a day that is not a time", envelope([wire({ joined_at: "yesterday" })])],
    ["a day that is missing", envelope([wire({ joined_at: undefined })])],
  ])("is null, never a shorter list, for %s", (_why, body) => {
    expect(waitlistRows(body)).toBeNull();
  });
});

describe("a row", () => {
  it("offers Admit while waiting, Send again once admitted or emailed, and nothing once signed up", () => {
    expect(WAITLIST_STATES.map((state) => [state, rowAction(state)])).toEqual([
      ["waiting", "admit"],
      ["admitted", "resend"],
      ["emailed", "resend"],
      ["signed_up", null],
    ]);
  });

  it("names every state", () => {
    expect(Object.keys(STATE_LABELS).sort()).toEqual([...WAITLIST_STATES].sort());
  });

  it("says the day it joined in UTC, or that it never joined", () => {
    const joined: WaitlistRow = { email: "a@example.com", joinedAt: "2026-10-12T23:30:00+00:00", state: "waiting" };
    expect(joinedLabel(joined)).toBe("Oct 12, 2026");
    expect(joinedLabel({ ...joined, joinedAt: null })).toBe("not on the waitlist");
  });

  it("counts only the addresses still waiting", () => {
    const rows: WaitlistRow[] = WAITLIST_STATES.map((state, i) => ({
      email: `${i}@example.com`,
      joinedAt: null,
      state,
    }));
    expect(waitingLine([...rows, { ...rows[0], email: "b@example.com" }])).toBe("2 still waiting");
    expect(waitingLine([])).toBe("0 still waiting");
  });

  it("says the read failed in one fixed sentence", () => {
    expect(WAITLIST_COPY.unreadable).toBe("We couldn't read the waitlist just now.");
  });
});

const answered = (outcome: string): BffResult => ({ ok: true, status: 200, data: { outcome } });
const refused = (error: string, status: number): BffResult => ({ ok: false, error, status, body: { error } });

describe("an admission's line", () => {
  it.each([
    [answered("admitted"), "Admitted. Their sign-up email is queued.", true],
    [answered("resent"), "Sent again. The email is queued, and its new link replaces the last.", true],
    [answered("admitted_before"), "Already admitted. Use Send again to email the link again.", true],
    [
      refused("signed_up", 409),
      "Already signed up: an account was created at or holds this address, or its link was used.",
      false,
    ],
    [refused("invalid_email", 400), "That isn't an address an admission can hold.", false],
    [
      refused("not_configured", 503),
      "Can't admit yet: the API has no WEB_APP_URL to build the email's link from.",
      false,
    ],
    [refused("not_ops", 403), "Only the site's operators can admit.", false],
    [
      refused("cross_site", 403),
      "That didn't come from this page, so nothing changed. Reload it and try again.",
      false,
    ],
    [refused("malformed_body", 400), "That didn't go through. Nothing changed — try again.", false],
  ])("is the answer's own (%#)", (result, text, ok) => {
    expect(admissionLine(result)).toEqual({ text, ok });
  });

  it.each([
    ["the route", refused("unauthenticated", 401)],
    ["the API", refused("http_401", 401)],
  ])("says the session was not accepted by %s, and that nothing changed", (_who, result) => {
    const { text, ok } = admissionLine(result);
    expect(ok).toBe(false);
    expect(text).toContain("You are not signed in");
    expect(text).toContain("Nothing changed.");
  });

  it.each([
    ["no answer", refused("unreachable", 0)],
    ["an unreachable API", refused("target_router_unreachable", 503)],
    ["a server error", refused("http_500", 500)],
  ])("after %s, says to look before trying again: it may have gone", (_why, result) => {
    expect(admissionLine(result)).toEqual({ text: LOST_ANSWER, ok: false });
  });

  it("names no outcome it does not know as one it does, and calls it no success", () => {
    expect(admissionLine(answered("toString"))).toEqual({ text: LOST_ANSWER, ok: false });
  });

  it.each([
    ["no outcome", {}],
    ["a null outcome", { outcome: null }],
    ["an empty outcome", { outcome: "" }],
    ["an outcome that is not a string", { outcome: 1 }],
    ["an outcome it does not know", { outcome: "approved" }],
    ["a refusal's reason in a success", { error: "malformed_response" }],
  ])("says a success that names %s could not be confirmed (#1740)", (_why, data) => {
    expect(admissionLine({ ok: true, status: 200, data })).toEqual({ text: LOST_ANSWER, ok: false });
  });

  // Written out, never derived from the module: only the three that leave the
  // address admitted went as asked, however the outcome arrives.
  const WENT_AS_ASKED = {
    admitted: true,
    resent: true,
    admitted_before: true,
    signed_up: false,
    invalid_email: false,
    not_configured: false,
  } as const;

  it("decides every outcome the vocabulary has", () => {
    expect(Object.keys(WENT_AS_ASKED).sort()).toEqual([...ADMIT_OUTCOMES].sort());
  });

  it.each(Object.entries(WENT_AS_ASKED))("says whether %s went as asked: %s", (outcome, ok) => {
    expect(admissionLine(answered(outcome)).ok).toBe(ok);
    expect(admissionLine(answered(outcome)).text).not.toBe(LOST_ANSWER);
  });
});

describe("an admission's outcome", () => {
  it.each([...ADMIT_OUTCOMES])("is %s when the answer names it", (outcome) => {
    expect(admissionOutcome({ outcome })).toBe(outcome);
    expect(admissionOutcome({ outcome, job_id: "j-1" })).toBe(outcome);
  });

  it.each([
    ["no answer", undefined],
    ["null", null],
    ["a list", []],
    ["a list holding an answer", [{ outcome: "admitted" }]],
    ["a list that carries an outcome", Object.assign([], { outcome: "admitted" })],
    ["a bare string naming an outcome", "admitted"],
    ["a number", 7],
    ["true", true],
    ["an empty object", {}],
    ["an empty outcome", { outcome: "" }],
    ["a null outcome", { outcome: null }],
    ["an outcome that is a number", { outcome: 1 }],
    ["an outcome that is a list", { outcome: ["admitted"] }],
    ["an outcome that is an object", { outcome: { admitted: true } }],
    ["an outcome it does not know", { outcome: "approved" }],
    ["a known outcome in capitals", { outcome: "Admitted" }],
    ["toString", { outcome: "toString" }],
    ["constructor", { outcome: "constructor" }],
    ["__proto__", JSON.parse('{"outcome":"__proto__"}')],
  ])("is null for %s", (_why, body) => {
    expect(admissionOutcome(body)).toBeNull();
  });
});

describe("the states", () => {
  it("are the API's `_state` answers", () => {
    const source = path.resolve(
      path.dirname(fileURLToPath(import.meta.url)),
      "../../../src/services/target/signup_admissions.py",
    );
    let python: string;
    try {
      python = readFileSync(source, "utf8");
    } catch (err) {
      throw new Error(`cannot read the API's waitlist states at ${source}: ${err}`);
    }
    const body = python.match(/^def _state\([\s\S]*?(?=^\S)/m)?.[0];
    expect(body, "signup_admissions._state").toBeDefined();
    const answers = [...body!.matchAll(/return "([a-z_]+)"/g)].map((m) => m[1]);
    expect(answers.sort()).toEqual([...WAITLIST_STATES].sort());
  });
});
