/**
 * The shapes the dashboard screens read from the target router.
 *
 * Every type below is read off the route that serves it and named for it.
 *
 * ── The server returns counts, not rows ────────────────────────────────────
 *
 * The server returns flat COUNT DICTS, not computed rows: one `stats` call
 * carries the counts, and each list a screen shows is `intents?state=` with
 * its own state set. So the derivations happen here, once, in `deriveX`
 * functions that are unit tested — rather than inline in six components where
 * they would drift.
 *
 * ── UNAVAILABLE is not zero, and the type system enforces it ───────────────
 *
 * The `times_posted` buckets have NO source on the target tier, so
 * they are `null`, NOT `0` and NOT omitted. The configured category mix has
 * one, the category-mix route, which the overview's mix card reads.
 *
 * `null` rather than optional is deliberate and load-bearing. An optional field
 * lets a consumer write `?? 0` and silently render a fabricated figure as a
 * real one. A non-optional `| null` makes the compiler stop at every consumer
 * until it says what it shows instead. Removing the null is a visible edit,
 * which is what makes serving the buckets a choice rather than a side effect
 * of another change.
 */

import { asApprovalPosition, type ApprovalPosition } from "./approvals";
import type { MediaProvenance } from "./archive-media";
import { NON_TERMINAL_STATES, TERMINAL_STATES as TERMINAL_STATE_LIST } from "./intents";
import { cardRows, type CategoryMixResponse, type WeightMode } from "./category-mix";
import {
  destinationsDiffer,
  sharePercents,
  sourceSubtitle,
  sourceTitle,
  targetLabel,
  type SourceMix,
} from "./source-mix";

// ── What the routes actually return ────────────────────────────────────────

/** `GET /api/v1/workspaces/{ws}/stats` — counted where the rows are. */
export type StatsResponse = {
  intents_by_state: Record<string, number>;
  media_by_state: Record<string, number>;
  media_never_posted: number;
  media_by_category: Record<string, number>;
  /**
   * Posts per connected folder over the stats window, keyed on `source_id`,
   * the key the category-mix route keys its weights on. A folder with no post
   * in the window has no key.
   */
  posted_by_source: Record<string, number>;
  posts_by_day: { local_date: string; count: number; cap: number }[];
  accounts: number;
  sources: number;
};

/**
 * The intent row and its envelope live in `intents.ts` — ONE contract. THIS
 * FILE DOES NOT RE-EXPORT THEM; import `Intent`, `IntentState` and
 * `IntentsResponse` from `@/lib/intents`.
 *
 * A second declaration or import path for one contract is how two copies
 * come to disagree: structural typing keeps both compiling while they drift
 *. The signpost is this paragraph, which cannot be imported.
 */

/** A row of `GET …/media?state=&never_posted=&limit=` — `_MEDIA_COLUMNS`. */
export type MediaRow = {
  id: string;
  source_id: string | null;
  /** The provider's id for the file; null for an archive item, whose is Instagram's id. */
  provider_file_ref: string | null;
  /**
   * An archive item reads `ARCHIVE_ITEM_NAME`: it is stored under that name, and
   * every read projects it, a row stored as `Instagram <Graph id>` included.
   */
  file_name: string;
  media_kind: string;
  mime_type: string | null;
  file_size: number | null;
  category: string | null;
  title: string | null;
  caption: string | null;
  tags: string[] | null;
  /** Whether the media has a thumbnail to fetch through `thumbnailSrc`. */
  has_thumbnail: boolean;
  /**
   * The thumbnail URL's version. A Drive item's is the same version for the
   * same picture; an archive item's is the generation and epoch the API will
   * answer for, and null while the item awaits observation again.
   */
  thumbnail_version: string | null;
  /**
   * Where an archive item came from; null for a Drive item. This read carries
   * it, and it is never a Graph id, a permalink or a caption.
   */
  provenance?: MediaProvenance | null;
  /** The link its stories ask a person to add by hand; shown only through `httpsHref`. */
  link_url: string | null;
  /**
   * Its Auto-approve flag: each of its stories is approved
   * automatically when served, whatever its folder's position. False until a
   * member turns it on. An API that does not send it yet leaves it out, and
   * `MediaCard` reads that as off.
   */
  auto_approve?: boolean;
  state: string;
  times_posted: number;
  last_posted_at: string | null;
  created_at: string;
};

export type MediaResponse = { media: MediaRow[]; limit: number };

/** `GET /api/v1/workspaces/{ws}` — `_CONFIG_COLUMNS`, the typed columns. */
export type WorkspaceConfig = {
  id: string;
  name: string;
  state: string;
  tz: string | null;
  posts_per_day: number | null;
  posting_hours_start: number | null;
  posting_hours_end: number | null;
  approval_mode: string | null;
  approval_ttl_minutes: number | null;
  dry_run_mode: boolean | null;
  is_paused: boolean | null;
  paused_at: string | null;
  repost_ttl_days: number | null;
  skip_ttl_days: number | null;
  caption_style: string | null;
  enable_ai_captions: boolean | null;
  api_publishing_enabled: boolean | null;
  offboarding_at: string | null;
  /** When an offboarding workspace can last be restored; server-computed. */
  restorable_until: string | null;
  /** The deployment's fallbacks for the columns `identity_and_tenancy.py` declares NULL, served
   * rather than retyped here — the settings cards show them and must not
   * hold a copy (the same reason as `restorable_until`). */
  defaults: { repost_ttl_days: number; skip_ttl_days: number };
  created_at: string;
  updated_at: string | null;
};

/** A row of `GET …/sources`. */
export type SourceRow = {
  id: string;
  provider: string;
  state: string;
  next_sync_at: string | null;
  last_sync_success_at: string | null;
  alerted_at: string | null;
  created_at: string;
  /** The Drive folder id the source reads (`config.folder_ref`). */
  folder_ref: string | null;
  /** The name the picker gave it (`config.folder_name`); null for a folder added by link. */
  folder_name: string | null;
  /** Removed under Integrations (a pause with a flag); false = connected. */
  removed?: boolean;
  /**
   * The folder's own approval position (`ck_source_approval_mode`), or
   * null for Workspace default: its stories follow the workspace's.
   * Typed as the wire carries it, as `WorkspaceConfig.approval_mode` is: an API
   * that does not send it yet leaves it out, and it may be a position this tier
   * does not know. `folderPositionOf` narrows it and shows either as unavailable.
   */
  approval_mode?: string | null;
};

export type SourcesResponse = { sources: SourceRow[] };

/**
 * `GET /workspaces/{ws}/drive` — the WORKSPACE's Google Drive grant (workspace Drive grant:
 * one per workspace, every folder under it), projected as the
 * destinations' credentials are. `none` = never connected; `expired` and
 * `revoked` = reconnect needed. Never a token.
 */
export type DriveStatus = {
  status: "none" | "active" | "expired" | "revoked" | string;
  connected_at: string | null;
};

export type DriveStatusResponse = { drive: DriveStatus };

// ── The state sets, named once ─────────────────────────────────────────────
//
// `?state=` takes a comma list validated against the closed `ck_intent_state`
// vocabulary, so a typo is a 422 rather than an empty list. These live here
// because three screens ask the same questions and a hand-written list at each
// call site is how two of them quietly diverge.

/** What a history tab means: the terminal outcomes. */
export const HISTORY_STATES = "posted,skipped,rejected";

/**
 * What the calendar draws under Posted: the stories that went out. A skipped
 * or rejected story has an outcome date too, and drawn in that lane it reads
 * as a post that happened. The sample workspace's calendar keeps the same set.
 */
export const POSTED_STATES = "posted";

/**
 * What a queue means: everything before a terminal outcome — all seven.
 *
 * The queue's read is `intents?state=`, which names no set of its own
 *, so the set is this file's. A card that undercounts without
 * disclosing is worse than one that errors, because nothing on the page
 * invites the reader to doubt it.
 *
 * Definition and contents agree: `intent-states-contract.test.ts` reads the
 * API's own `INTENT_STATES` and fails unless QUEUE and TERMINAL together
 * account for every member. Documenting an exclusion only helps a reader of
 * THIS file; a count is read on the dashboard.
 *
 * The distinction that `publishing` is "in flight rather than queued" is real,
 * but it is not one the label "In Queue" draws for a reader, and holding it
 * costs a wrong number. If the queue should ever exclude a non-terminal state,
 * the contract test makes that a deliberate, visible edit.
 *
 * DERIVED, NOT RE-TYPED. The members are `NON_TERMINAL_STATES` in
 * `intents.ts`; this is the `?state=` spelling of them. A state added there
 * lands here without an edit, which is the failure mode the paragraph above
 * describes, closed at the source rather than watched for.
 */
export const QUEUE_STATES = NON_TERMINAL_STATES.join(",");

/**
 * The terminal outcomes — the other half of the partition. `intent_ledger.py`
 * labels these `TERMINAL` in `ck_intent_state` itself, so this is the schema's
 * classification rather than one invented here.
 *
 * Deliberately a SUPERSET of `HISTORY_STATES`: `expired`, `failed` and
 * `cancelled` are terminal, and the history tab does not show them.
 * Naming them here is what lets the contract test account for all thirteen
 * states rather than for thirteen minus whatever the history tab happens to
 * render.
 *
 * DERIVED from `intents.TERMINAL_STATES`, which mirrors
 * `command_executors.TERMINAL_STATES` (Python). The superset relationship
 * with `HISTORY_STATES` above is this tier's call.
 */
export const TERMINAL_STATES = TERMINAL_STATE_LIST.join(",");

/**
 * The one queue member an operator has to act on personally.
 *
 * Named because the calendar surfaces it separately when non-zero: it is
 * reached via the poison ladder once publish retries exhaust, it is
 * operator-owned, and it does not clear itself. Counting it is necessary but
 * not sufficient — a stuck intent folded anonymously into a queue depth is
 * accurate and still tells nobody to go and look at it.
 */
export const REVIEW_REQUIRED_STATE = "review_required";

// ── Derived views ──────────────────────────────────────────────────────────

/**
 * A figure no target route serves. NEVER rendered as a number.
 *
 * Greppable on purpose: `grep -rn -w Unavailable landing/src/` finds
 * every place the screens type or render a figure as unavailable.
 */
export type Unavailable = null;

export type SummaryView = {
  posted: number;
  skipped: number;
  rejected: number;
  failed: number;
  total: number;
  /**
   * posted / (posted + failed) — publish outcomes only.
   *
   * `Unavailable` when there have been NO publish attempts. A rate over an
   * empty divisor is not a low rate, and `0%` on this card reads as a verdict
   * on the workspace rather than as the absence of anything to judge. Same
   * rule as the unsourced columns, different cause: this one resolves
   * itself the moment one publish is attempted.
   */
  success_rate: number | Unavailable;
  /** `Unavailable` when there is no window to average over — see above. */
  avg_per_day: number | Unavailable;
};

/** One connected folder on the overview's mix card. */
export type FolderMixView = {
  sourceId: string;
  name: string;
  mode: WeightMode;
  /**
   * The share of posts the mix plans for this folder, in percent: the API's
   * `effective`, which Settings shows as "posts about".
   */
  planned: number;
  /**
   * Its share of every post in the stats window, in percent. `Unavailable`
   * when the window holds no post: a share of nothing is not 0%.
   */
  posted: number | Unavailable;
};

export type FolderMix = {
  folders: FolderMixView[];
  /**
   * Every post in the window: the divisor of each posted share. `Unavailable`
   * when the API sent no `posted_by_source`.
   */
  total: number | Unavailable;
  /** Posts in the window from folders no longer connected: in `total`, on no row. */
  fromRemoved: number;
};

export type PoolHealthView = {
  total_active: number;
  never_posted: number;
  by_category: { name: string; count: number }[];
  /**: `times_posted` buckets have no target-side source. */
  posted_once: number | Unavailable;
  /**: `times_posted` buckets have no target-side source. */
  posted_multiple: number | Unavailable;
  /**
   * Eligible to post now — available, and past its repost TTL.
   *
   * No target route serves it: `stats` serves `media_by_state`,
   * which is a different axis, and the TTL lives on the workspace config
   * rather than the item.
   */
  eligible_for_posting: number | Unavailable;
};

const sum = (d: Record<string, number>) =>
  Object.values(d).reduce((a, b) => a + b, 0);

/** Headline counts from `intents_by_state`, with the rates derived here once. */
export function deriveSummary(stats: StatsResponse): SummaryView {
  const s = stats.intents_by_state ?? {};
  const posted = s.posted ?? 0;
  const failed = s.failed ?? 0;
  const attempts = posted + failed;
  const days = stats.posts_by_day?.length ?? 0;
  const postedOverWindow = (stats.posts_by_day ?? []).reduce(
    (a, r) => a + r.count,
    0,
  );
  return {
    posted,
    failed,
    skipped: s.skipped ?? 0,
    rejected: s.rejected ?? 0,
    total: sum(s),
    // Publish outcomes only (/): Telegram delivery is not an intent
    // state, so there is nothing of the other kind in the divisor.
    success_rate: attempts === 0 ? null : posted / attempts,
    // From the cap ledger, not from a bounded list. Zero days means no window
    // rather than a zero average: `Unavailable`, like the rate above.
    avg_per_day: days === 0 ? null : postedOverWindow / days,
  };
}

/**
 * The mix card: every connected folder from the category-mix route, in its
 * order, beside its share of the posts `stats` counted over the window. The
 * join is on `source_id`, never the folder's name, which is a label a rename
 * changes.
 */
export function deriveFolderMix(
  stats: StatsResponse,
  mix: CategoryMixResponse,
): FolderMix {
  // An answer without `posted_by_source` is no window to share out of, not an
  // empty one: `Unavailable`, never 0.
  const posted = stats.posted_by_source as Record<string, number> | undefined;
  const total = posted === undefined ? null : sum(posted);
  const folders = cardRows(mix).map((row) => ({
    sourceId: row.sourceId,
    name: row.name,
    mode: row.mode,
    planned: row.effective,
    posted:
      posted === undefined || total === null || total === 0
        ? null
        : ((posted[row.sourceId] ?? 0) / total) * 100,
  }));
  const listed = folders.reduce((a, f) => a + (posted?.[f.sourceId] ?? 0), 0);
  return { folders, total, fromRemoved: total === null ? 0 : total - listed };
}

/** One source on the overview's mix card of a converted workspace. */
export type SourceShareView = {
  sourceId: string;
  /** "Your Instagram", or the folder's name. */
  title: string;
  /** "@handle · Instagram", or "Google Drive": the handle is in the row's text. */
  subtitle: string;
  /** Its workspace target as shown: "50%", "<0.1%" or "Off". */
  target: string;
  enabled: boolean;
  /** Its share of the window's posts, in percent; `Unavailable` as above. */
  posted: number | Unavailable;
};

export type SourceShares = {
  sources: SourceShareView[];
  total: number | Unavailable;
  /** Posts in the window from sources no longer connected. */
  fromRemoved: number;
  /** Every source deliberately Off: a choice, never "nothing connected". */
  allOff: boolean;
  /**
   * The listed accounts see different mixes (`destinationsDiffer`): then a
   * workspace target is no one account's share. Several accounts that draw on
   * the same sources have one mix, and this is false.
   */
  accountsDiffer: boolean;
};

/**
 * The overview's mix card for protocol 2 (`source-mix.ts`): sources, not
 * folders, each with its workspace target share as the settings card shows it
 * (decimal strings to one place, Off as Off), beside its share of what the mix
 * picked (`stats.posted_by_source`, counted exactly as for protocol 1).
 * `deriveFolderMix` reads with protocol-1 rules, which would print "NaN%" for
 * a share that is a string and read an Off source as explicit.
 */
export function deriveSourceShares(stats: StatsResponse, mix: SourceMix): SourceShares {
  const posted = stats.posted_by_source as Record<string, number> | undefined;
  const total = posted === undefined ? null : sum(posted);
  const percents = sharePercents(mix.rows.map((row) => row.targetShare));
  const sources = mix.rows.map((row, i) => ({
    sourceId: row.sourceId,
    title: sourceTitle(row),
    subtitle: sourceSubtitle(row),
    target: targetLabel(row, percents[i]),
    enabled: row.enabled,
    posted:
      posted === undefined || total === null || total === 0
        ? null
        : ((posted[row.sourceId] ?? 0) / total) * 100,
  }));
  const listed = sources.reduce((a, s) => a + (posted?.[s.sourceId] ?? 0), 0);
  return {
    sources,
    total,
    fromRemoved: total === null ? 0 : total - listed,
    allOff: sources.length > 0 && !sources.some((s) => s.enabled),
    accountsDiffer: destinationsDiffer(mix.destinations),
  };
}

/** Pool health, as far as `stats` can answer it. */
export function derivePoolHealth(stats: StatsResponse): PoolHealthView {
  const byState = stats.media_by_state ?? {};
  return {
    total_active: byState.available ?? 0,
    never_posted: stats.media_never_posted ?? 0,
    by_category: Object.entries(stats.media_by_category ?? {})
      .filter(([name]) => name !== "")
      .sort(([a], [b]) => a.localeCompare(b))
      .map(([name, count]) => ({ name, count })),
    posted_once: null,
    posted_multiple: null,
    eligible_for_posting: null,
  };
}

/**
 * What the Settings screen can HONESTLY show from the routes that exist.
 *
 * ── Why this is a derivation and not four reads spread across the page ──────
 *
 * Coalescing a field (`setup.X ?? default`) is the dangerous half, and it is
 * NOT what the hard bail protects against: the bail catches a fetch that
 * FAILED, while a defaulted field arrives inside a response that SUCCEEDED.
 * `media_sync_enabled ?? false` renders "Auto-sync disabled" — a claim about
 * the workspace manufactured from a missing column, which is the exact harm
 * `settings/page.tsx` warns about in its own comment.
 *
 * So every field is resolved in one place, and a field with no source is
 * `Unavailable` — never a default. The type is non-optional `null`: an
 * optional field invites `?? 0` at the consumer, while a non-optional null
 * makes the compiler stop at every consumer until it says what it shows
 * instead. Sourcing one later is then a VISIBLE type change rather than a
 * silent behaviour change.
 */
export type SettingsView = {
  // ── From the workspace row (`GET /workspaces/{ws}`) ──────────────────────
  posts_per_day: number | null;
  posting_hours_start: number | null;
  posting_hours_end: number | null;
  /** IANA zone the window is read in (`workspaces.tz`, `04_functions.sql` `fn_next_slot`). */
  tz: string | null;
  is_paused: boolean | null;
  dry_run_mode: boolean | null;
  /** The workspace row calls this `api_publishing_enabled`. Renamed once, here. */
  enable_instagram_api: boolean | null;
  /**
   * The workspace's approval position: what its stories do when they
   * are served, unless a folder has its own. Null when the row's value is not
   * one of the three, never Ask every time in its place.
   */
  approval_mode: ApprovalPosition | null;
  enable_ai_captions: boolean | null;
  repost_ttl_days: number | null;
  skip_ttl_days: number | null;
  /** Carried but NOT rendered: no code in the tier reads `caption_style`, so
   * its card is not shown. */
  caption_style: string | null;
  /** The deployment's fallbacks for the two NULL-on-purpose TTL columns.
   * Served, never retyped here. */
  defaults: { repost_ttl_days: number; skip_ttl_days: number };

  // ── From `sources` and `stats` ───────────────────────────────────────────
  gdrive_connected: boolean;
  media_source_type: string | null;
  media_source_state: string | null;
  media_count: number;

  // ── NO SOURCE ON THE TARGET TIER. `Unavailable`, never a default. ────────
  //
  // Greppable on purpose (see `Unavailable`): these are the Settings fields no
  // target route serves.
  show_verbose_notifications: Unavailable;
  send_lifecycle_notifications: Unavailable;
  media_sync_enabled: Unavailable;
  gdrive_email: Unavailable;
  media_source_root: Unavailable;
};

/**
 * Settings from the four routes that exist, with the gaps left as gaps.
 *
 * `media_source_root` is unavailable for a reason worth naming, because it
 * looks sourceable and is not: it lives in `media_sources.config.root_name`,
 * and `_SOURCE_COLUMNS` does not return `config`. Reading the provider and
 * guessing the folder from it would be the defaulting this type exists to stop.
 */
export function deriveSettings(
  config: WorkspaceConfig,
  sources: SourceRow[],
  stats: StatsResponse,
  drive: DriveStatus | null = null,
): SettingsView {
  // The first Drive source, whatever its state. State is carried rather than
  // flattened into the boolean: "connected but erroring" and "not connected"
  // are different facts with different remedies.
  const firstSource = sources.find((s) => s.provider === "gdrive") ?? null;
  const byState = stats.media_by_state ?? {};

  return {
    posts_per_day: config.posts_per_day,
    posting_hours_start: config.posting_hours_start,
    posting_hours_end: config.posting_hours_end,
    tz: config.tz,
    is_paused: config.is_paused,
    dry_run_mode: config.dry_run_mode,
    enable_instagram_api: config.api_publishing_enabled,
    approval_mode: asApprovalPosition(config.approval_mode),
    enable_ai_captions: config.enable_ai_captions,
    repost_ttl_days: config.repost_ttl_days,
    skip_ttl_days: config.skip_ttl_days,
    caption_style: config.caption_style,
    defaults: config.defaults,

    /*
     * This is the CREDENTIAL's answer rather than the source row's: a
     * folder pasted by link can exist before any grant does. The credential
     * is the WORKSPACE's (workspace Drive grant) — one grant, every folder under it — so
     * the answer comes from `GET /workspaces/{ws}/drive`, and a source row
     * says nothing about it either way.
     *
     * BOUND, so no caller reads more confidence into this than it has: an
     * UNDECRYPTABLE grant reads `active`. The status query cannot know a
     * payload fails to decrypt without attempting decryption, so this means
     * "a usable-looking grant exists", not "a request will succeed".
     */
    gdrive_connected: drive?.status === "active",
    media_source_type: firstSource?.provider ?? null,
    media_source_state: firstSource?.state ?? null,
    // What is connected: a removed folder's rows are retired (`removed`) and
    // out of the library until the folder, or another one listing the same
    // bytes, brings them back.
    media_count: byState.available ?? 0,

    show_verbose_notifications: null,
    send_lifecycle_notifications: null,
    media_sync_enabled: null,
    gdrive_email: null,
    media_source_root: null,
  };
}

// The target returns `handle`/`state`, not `instagram_username`/`is_active`,
// so a row is a `Destination`.
export type AccountsResponse = { accounts: import("./types").Destination[] };
