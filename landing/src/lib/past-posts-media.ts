/**
 * What the web says about an item that came from a connected account's own
 * past posts.
 *
 * Every read that draws an item's picture carries its provenance beside the
 * file (the media library's, the Queue's, the calendar's month and its planned
 * stories): the provider, the handle of the account that posted it and when. The
 * web says where a post came from by those, and never by what else the API holds
 * of it: a post's id, link and caption are not for a screen. An item is stored
 * under the neutral `PAST_POSTS_ITEM_NAME`, and the id Instagram gave the post stays
 * in `provider_file_ref`. Every read projects that name and a null
 * `provider_file_ref` (`vocabulary.PAST_POSTS_ITEM_NAME`, held to this by
 * `wire-contract.test.ts`), a row stored as `Instagram <Graph id>` included;
 * `withPastPostsName` is the web's own guard, for a row that arrives without it.
 */

import { POSTING_MIX_HREF } from "./fragment-scroll";
import { formatInZone, instant } from "./zoned-dates";

/** The provider of a past-posts source, as `provenance.provider` and a source row say it. */
export const PAST_POSTS_PROVIDER = "instagram_past_posts";

/** `provenance` of a media read: what a past-posts item may be said to come from. */
export type MediaProvenance = {
  provider: string;
  /** The handle of the account that posted it; null when that account carries none. */
  origin_handle: string | null;
  /** When it was posted; null while the item awaits observation again. */
  original_at: string | null;
  /** Null for an ordinary post; count is unknown when the children read was truncated. */
  slide?: { post_ref: string; position: number; count: number | null } | null;
};

/** A row that may carry provenance: Drive rows carry null, and a read without it leaves it out. */
type Provenanced = { provenance?: MediaProvenance | null };

/** The provenance of a past-posts row, or null for any other row. */
export function pastPostsProvenance(row: Provenanced): MediaProvenance | null {
  return row.provenance?.provider === PAST_POSTS_PROVIDER ? row.provenance : null;
}

export function isPastPostsMedia(row: Provenanced): boolean {
  return pastPostsProvenance(row) !== null;
}

/** What a past-posts item is called where a file name would be, as every screen, dialog and label says it. */
export const PAST_POSTS_ITEM_NAME = "Instagram post";
/** Mirrors the server's `PAST_POSTS_SLIDE_NAME` format. */
export const PAST_POSTS_SLIDE_NAME = "Instagram post, slide {position} of {count}";
export const PAST_POSTS_SLIDE_NAME_UNKNOWN = "Instagram post, slide {position}";

export function pastPostsSlideName(slide: NonNullable<MediaProvenance["slide"]>): string {
  return slide.count === null
    ? PAST_POSTS_SLIDE_NAME_UNKNOWN.replace("{position}", String(slide.position))
    : PAST_POSTS_SLIDE_NAME.replace("{position}", String(slide.position)).replace("{count}", String(slide.count));
}

export function slideLabel(slide: NonNullable<MediaProvenance["slide"]>): string {
  return slide.count === null ? `Slide ${slide.position}` : `Slide ${slide.position} of ${slide.count}`;
}

/**
 * The row to draw: a past-posts item under its own name, whatever the row says.
 * The API already sends it, so this only acts on a row that carries
 * `Instagram <Graph id>`. Any other row is returned as it came.
 */
export function withPastPostsName<T extends { file_name: string } & Provenanced>(row: T): T {
  const origin = pastPostsProvenance(row);
  if (!origin) return row;
  if (origin.slide) return { ...row, file_name: pastPostsSlideName(origin.slide) };
  // Queue and calendar reads may send only the server's slide name.
  if (row.file_name.startsWith("Instagram post, slide ")) return row;
  return { ...row, file_name: PAST_POSTS_ITEM_NAME };
}

/**
 * What a past-posts item's card says of its media, and no more: it is checked
 * when it is used. It does NOT say the item matches. The library lists every
 * item the account's past-posts source holds, whatever a source's settings leave out of
 * what is picked from (too recent, credited, unknown attribution), so a card
 * that said "matching" would be a claim the list does not support. Never
 * verified, never ready, never published.
 *
 * The Posting mix card says the same words (`MEDIA_CHECKED_COPY`, held equal by
 * `past-posts-media.test.ts`): that module reaches the API door, so this one cannot
 * import it.
 */
export const PAST_POSTS_MEDIA_CHECKED_COPY = "Media is checked when used.";

/** What a tile says in place of a picture that did not come. */
export const PREVIEW_UNAVAILABLE_COPY = "Instagram preview unavailable";

/** The handle without any `@`, as `bareHandle` of `source-mix.ts` reads it; that module reaches the API door, so this one cannot import it. */
function handleOf(value: string | null): string | null {
  const handle = value?.trim().replace(/^@+/, "").trim();
  return handle ? handle : null;
}

/** The day a post went up on `tz`'s clock, or null for a time that is not one. */
function postedOn(at: string, tz: string): string | null {
  if (Number.isNaN(instant(at).getTime())) return null;
  return formatInZone(at, tz, { month: "short", day: "numeric", year: "numeric" });
}

/**
 * "From @handle · Instagram, posted Jan 1, 2020": the handle and the day, each
 * left out when the read has none. The day is on `tz`'s clock, the workspace's,
 * so the server and the browser say the same one.
 */
export function provenanceLabel(provenance: MediaProvenance, tz = "UTC"): string {
  const handle = handleOf(provenance.origin_handle);
  const from = handle ? `From @${handle} · Instagram` : "From Instagram";
  const day = provenance.original_at ? postedOn(provenance.original_at, tz) : null;
  return day ? `${from}, posted ${day}` : from;
}

/**
 * Past-posts previews are listed this many to a page. The media read has a limit
 * and no offset, so a page is a window over the rows already read.
 */
export const PAST_POSTS_PAGE_SIZE = 24;

/** The rows of page `page` (counted from 0, held to the pages there are) and how many pages there are. */
export function pageOf<T>(
  rows: readonly T[],
  page: number,
  size = PAST_POSTS_PAGE_SIZE,
): { rows: T[]; page: number; pages: number } {
  const pages = Math.max(1, Math.ceil(rows.length / size));
  const at = Math.min(Math.max(0, Math.trunc(page) || 0), pages - 1);
  return { rows: rows.slice(at * size, (at + 1) * size), page: at, pages };
}

/** Whether the workspace uses its past posts as a source: on, or Off. Absent when it has none. */
export type PastPosts = "on" | "off";

/**
 * The past-posts source of a converted workspace's mix, or null when the mix
 * is not one (protocol 1, or not read) or holds none. On when any Instagram
 * source is on, Off when every one is.
 */
export function pastPostsOf(
  mix: { rows: readonly { provider: string; enabled: boolean }[] } | null,
): PastPosts | null {
  const rows = mix?.rows.filter((row) => row.provider === PAST_POSTS_PROVIDER) ?? [];
  if (rows.length === 0) return null;
  return rows.some((row) => row.enabled) ? "on" : "off";
}

/**
 * What an empty library says. A workspace with past posts as a source is not
 * told to connect Google Drive: its posts arrive as they are indexed, or it has
 * them Off, and Drive is one more source it may add. It does not say indexing
 * is running, which the mix does not tell. Every other workspace is told to
 * connect Google Drive.
 */
export function emptyLibrary(pastPosts: PastPosts | null): {
  title: string;
  description: string;
  action: { href: string; label: string };
} {
  const title = "No media items found";
  if (pastPosts === "on") {
    return {
      title,
      description:
        "Your past posts are on, so they appear here as they are indexed. Google Drive is optional.",
      action: { href: POSTING_MIX_HREF, label: "Open Posting mix" },
    };
  }
  if (pastPosts === "off") {
    return {
      title,
      description:
        "Your past posts are Off, so none are indexed. Turn them on in Posting mix. Google Drive is optional.",
      action: { href: POSTING_MIX_HREF, label: "Open Posting mix" },
    };
  }
  return {
    title,
    description: "Connect Google Drive and pick the folders to sync your content library.",
    action: { href: "/dashboard/settings?tab=integrations", label: "Set up Google Drive" },
  };
}
