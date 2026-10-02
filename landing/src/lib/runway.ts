import { destinationName } from "./destination";
import type { Destination } from "./types";

/**
 * Days of content left per account (#1478), as `GET /workspaces/{ws}/runway`
 * returns it.
 *
 * Counted on the server by the planner's own rule (`content_runway.runway`):
 * the files it could pick for the account now, over the posts per day it
 * spends them at. Nothing here recounts — a second definition of "eligible"
 * is exactly what the server's shared pool exists to prevent.
 */
export interface RunwayAccount
  extends Pick<Destination, "id" | "handle" | "display_name" | "state"> {
  /** Whether the clock posts for it now; a runway not being spent has no end. */
  posting: boolean;
  /** The account's own posts per day, else its workspace's. */
  posts_per_day: number;
  /** The files the planner could pick for this account now. */
  eligible: number;
  /**
   * Whole days: `eligible` over `posts_per_day`, rounded down on the server.
   * Null when the account is not posting.
   */
  days_left: number | null;
  /** Below the warning level: the notice's own test, decided on the server. */
  low: boolean;
}

export interface RunwayResponse {
  /** The warning level, in days: an account below it has been or will be told. */
  below_days: number;
  accounts: RunwayAccount[];
}

/** One account's line on the Overview. */
export interface RunwayRow {
  key: string;
  name: string;
  /** "About 6 days", "Less than a day", "No content left", or "Not posting". */
  headline: string;
  /** The arithmetic behind the headline: "20 files at 3 a day". */
  detail: string;
  low: boolean;
}

function files(count: number): string {
  return count === 1 ? "1 file" : `${count} files`;
}

/**
 * The server's whole days, as they are. It counts them down once, so a
 * part-day is never promised and nothing here rounds. Zero days is a part-day
 * only while a file is left, which the days cannot show: `deriveRunway`,
 * which has the files, says when there is none.
 */
export function runwayHeadline(daysLeft: number | null): string {
  if (daysLeft === null) return "Not posting";
  if (daysLeft < 1) return "Less than a day";
  return daysLeft === 1 ? "About 1 day" : `About ${daysLeft} days`;
}

/**
 * One row per account, in the server's order. An account the clock posts for
 * with no file at all has no content left rather than less than a day of it:
 * nothing is there to post, which is also how an account reads before any
 * Drive folder is connected.
 */
export function deriveRunway(runway: RunwayResponse): RunwayRow[] {
  return runway.accounts.map((account) => ({
    key: account.id,
    name: destinationName(account),
    headline:
      account.posting && account.eligible === 0
        ? "No content left"
        : runwayHeadline(account.days_left),
    detail: account.posting
      ? `${files(account.eligible)} at ${account.posts_per_day} a day`
      : `${files(account.eligible)} ready`,
    low: account.low,
  }));
}
