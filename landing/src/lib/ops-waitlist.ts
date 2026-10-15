import { callBff, postJson, type BffResult } from "./bff";
import { notAuthenticatedCopy } from "./refusal-copy";
import { formatInZone, instant } from "./zoned-dates";

/**
 * The owner area's waitlist (#1723): what its page reads, server-side
 * (`GET /api/v1/ops/waitlist`), and what its actions post (`POST
 * /api/admissions`, which relays to `POST /api/v1/admissions`). The API
 * decides who may do either (`require_ops`) and what an address is; this
 * module reads its answers.
 */

/** Where a listed address stands: `signup_admissions._state`'s answers (pinned). */
export const WAITLIST_STATES = ["waiting", "admitted", "emailed", "signed_up"] as const;
export type WaitlistState = (typeof WAITLIST_STATES)[number];

export type WaitlistRow = {
  email: string;
  /** When it joined the waitlist; null for an admission made off it. */
  joinedAt: string | null;
  state: WaitlistState;
};

function isState(value: unknown): value is WaitlistState {
  return (WAITLIST_STATES as readonly unknown[]).includes(value);
}

function isInstant(value: unknown): value is string {
  return typeof value === "string" && !Number.isNaN(instant(value).getTime());
}

/**
 * The API's waitlist envelope as rows, or null when the answer is not one. A
 * row that cannot be read fails the read whole: a list short of a row would
 * read as complete.
 */
export function waitlistRows(body: unknown): WaitlistRow[] | null {
  const rows = (body as { data?: { rows?: unknown } } | null)?.data?.rows;
  if (!Array.isArray(rows)) return null;
  const out: WaitlistRow[] = [];
  for (const row of rows as unknown[]) {
    const { email, joined_at: joinedAt, state } = (row ?? {}) as Record<string, unknown>;
    if (typeof email !== "string" || !isState(state)) return null;
    if (joinedAt !== null && !isInstant(joinedAt)) return null;
    out.push({ email, joinedAt, state });
  }
  return out;
}

/** A row's one action: Admit a waiting address, Send again an admitted or emailed one, none once it has signed up. */
export function rowAction(state: WaitlistState): "admit" | "resend" | null {
  switch (state) {
    case "waiting":
      return "admit";
    case "admitted":
    case "emailed":
      return "resend";
    case "signed_up":
      return null;
  }
}

export const STATE_LABELS: Record<WaitlistState, string> = {
  waiting: "Waiting",
  admitted: "Admitted",
  emailed: "Emailed",
  signed_up: "Signed up",
};

export const WAITLIST_COPY = {
  title: "Waitlist",
  description: "Everyone on the waitlist, and anyone admitted off it, newest first.",
  unreadable: "We couldn't read the waitlist just now.",
  empty: "No one is on the waitlist yet.",
  offList: "not on the waitlist",
  admitHeading: "Admit an address",
  admitHint:
    "On the waitlist or not. They're emailed a one-time link to sign up with any Google account.",
  admitLabel: "Email address",
  admit: "Admit",
  admitting: "Admitting…",
  resend: "Send again",
  resending: "Sending…",
} as const;

/** How many of the listed addresses are still waiting to be admitted. */
export function waitingLine(rows: readonly WaitlistRow[]): string {
  const waiting = rows.filter((row) => row.state === "waiting").length;
  return `${waiting} still waiting`;
}

const JOINED: Intl.DateTimeFormatOptions = { month: "short", day: "numeric", year: "numeric" };

/** The day an address joined, in UTC, or that it never did. */
export function joinedLabel(row: WaitlistRow): string {
  return row.joinedAt === null ? WAITLIST_COPY.offList : formatInZone(row.joinedAt, "UTC", JOINED);
}

/** The longest address an admission can hold, in characters (`vocabulary.MAX_EMAIL_LENGTH`, pinned). */
export const MAX_EMAIL_CHARS = 254;

/** What admitting an address can come to (`vocabulary.ADMIT_OUTCOMES`, pinned). */
export const ADMIT_OUTCOMES = [
  "admitted",
  "resent",
  "admitted_before",
  "signed_up",
  "invalid_email",
  "not_configured",
] as const;
type AdmitOutcome = (typeof ADMIT_OUTCOMES)[number];

/** Admit *email*, or with *resend* email it again: one call to this tier's route. */
export function postAdmission(email: string, resend: boolean): Promise<BffResult> {
  return callBff("/api/admissions", postJson({ email, resend }));
}

/** The line a control leaves after its answer, and whether it went as asked. */
export type AdmissionLine = { text: string; ok: boolean };

const OUTCOME_LINES: Record<AdmitOutcome, AdmissionLine> = {
  admitted: { text: "Admitted. Their sign-up email is queued.", ok: true },
  resent: { text: "Sent again. The email is queued, and its new link replaces the last.", ok: true },
  admitted_before: { text: "Already admitted. Use Send again to email the link again.", ok: true },
  signed_up: {
    text: "Already signed up: an account was created at or holds this address, or its link was used.",
    ok: false,
  },
  invalid_email: { text: "That isn't an address an admission can hold.", ok: false },
  not_configured: {
    text: "Can't admit yet: the API has no WEB_APP_URL to build the email's link from.",
    ok: false,
  },
};

/**
 * Not a refusal: the admission may have been made and its email queued, so it
 * says to look before trying again. Two Send agains are two emails. An answer
 * that names no outcome this tier knows says it too (#1740).
 */
export const LOST_ANSWER =
  "We couldn't confirm whether that went through. Reload the page before trying again.";

function isOutcome(value: unknown): value is AdmitOutcome {
  return (ADMIT_OUTCOMES as readonly unknown[]).includes(value);
}

/**
 * The outcome an admission's answer names, or null when it names none this
 * tier knows: the answer must be an object, not null and not a list, whose
 * `outcome` is one of `ADMIT_OUTCOMES` (#1740). The route reads the API's
 * answer through it and the page reads the route's, so an answer that cannot
 * be read is never taken for a success.
 */
export function admissionOutcome(body: unknown): AdmitOutcome | null {
  if (typeof body !== "object" || body === null || Array.isArray(body)) return null;
  const { outcome } = body as { outcome?: unknown };
  return isOutcome(outcome) ? outcome : null;
}

/**
 * The line for one answer of `postAdmission`: the outcome's, or the refusal's.
 * A success that names no outcome this tier knows is no success: like an
 * answer that never came, it says to look before trying again.
 */
export function admissionLine(result: BffResult): AdmissionLine {
  if (result.ok) {
    const outcome = admissionOutcome(result.data);
    return outcome === null ? { text: LOST_ANSWER, ok: false } : OUTCOME_LINES[outcome];
  }
  if (isOutcome(result.error)) return OUTCOME_LINES[result.error];
  if (result.error === "not_ops") return { text: "Only the site's operators can admit.", ok: false };
  if (result.status === 401) return { text: notAuthenticatedCopy("Nothing changed."), ok: false };
  if (result.status === 0 || result.status >= 500) return { text: LOST_ANSWER, ok: false };
  if (result.error === "cross_site") {
    return { text: "That didn't come from this page, so nothing changed. Reload it and try again.", ok: false };
  }
  return { text: "That didn't go through. Nothing changed — try again.", ok: false };
}
