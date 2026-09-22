/**
 * The shapes the dashboard screens read from the target router (#1044 repoint).
 *
 * These are no longer guesses. The previous version said outright that it was
 * PROVISIONAL — "derived from what the components consume today, which is
 * evidence of what the legacy backend sent, not a specification of what the
 * target router will send". The routes have landed, so every type below is read
 * off the route that serves it and named for it.
 *
 * ── The shape changed, not just the path ───────────────────────────────────
 *
 * This was described as a rename. It is not: the server returns flat COUNT
 * DICTS where the legacy backend returned computed rows. `analytics`,
 * `analytics/categories` and `init`'s media count collapse into one `stats`
 * call; `history-detail`, `queue-detail` and `analytics/schedule-preview` are
 * all `intents?state=` with different state sets. So the derivations that used
 * to happen server-side happen here, once, in `deriveX` functions that are unit
 * tested — rather than inline in six components where they would drift.
 *
 * ── UNAVAILABLE is not zero, and the type system enforces it ───────────────
 *
 * Two figures the old screens showed have NO source on the target tier
 * (#1048): the configured category mix, and the `times_posted` buckets. Chris
 * rules on whether to drop them or serve them; until then they are `null`, NOT
 * `0` and NOT omitted.
 *
 * `null` rather than optional is deliberate and load-bearing. An optional field
 * lets a consumer write `?? 0` and silently render a fabricated figure as a
 * real one — which is the same defect as the dashboard of zeros, one layer
 * down. A non-optional `| null` makes the compiler stop at every consumer until
 * it says what it shows instead. Removing the null is a visible edit, which is
 * what makes the decision Chris's rather than a side effect of this PR.
 */

import { NON_TERMINAL_STATES, TERMINAL_STATES as TERMINAL_STATE_LIST } from "./intents";

// ── What the routes actually return ────────────────────────────────────────

/** `GET /api/v1/workspaces/{ws}/stats` — counted where the rows are. */
export type StatsResponse = {
  intents_by_state: Record<string, number>;
  media_by_state: Record<string, number>;
  media_never_posted: number;
  media_by_category: Record<string, number>;
  posted_by_category: Record<string, number>;
  posts_by_day: { local_date: string; count: number; cap: number }[];
  accounts: number;
  sources: number;
};

/**
 * The intent row and its envelope live in `intents.ts` — ONE contract. THIS
 * FILE DOES NOT RE-EXPORT THEM; import `Intent`, `IntentState` and
 * `IntentsResponse` from `@/lib/intents`.
 *
 * This file used to declare a second `IntentRow`/`IntentsResponse` pair for
 * the three screens that read `?state=`. Structural typing kept both
 * compiling while they drifted: this copy was missing `account_handle` and
 * `account_display_name` (served by `_INTENT_COLUMNS`), and typed
 * `ig_account_id`, `schedule_slot_at` and `approval_mode` as nullable where
 * the server never sends null. #1338 moved every screen onto the owner and
 * left a type-only re-export here as a signpost; it ended that change with
 * zero importers, and a second import path for one contract is how the two
 * copies came to disagree in the first place. The signpost is this
 * paragraph, which cannot be imported.
 */

/** A row of `GET …/media?state=&never_posted=&limit=` — `_MEDIA_COLUMNS`. */
export type MediaRow = {
  id: string;
  source_id: string | null;
  provider_file_ref: string | null;
  file_name: string;
  media_kind: string;
  mime_type: string | null;
  file_size: number | null;
  category: string | null;
  title: string | null;
  caption: string | null;
  tags: string[] | null;
  thumbnail_url: string | null;
  state: string;
  times_posted: number;
  last_posted_at: string | null;
  created_at: string;
};

export type MediaResponse = { media: MediaRow[]; limit: number };

/** `GET /api/v1/workspaces/{ws}` — `_CONFIG_COLUMNS`, the `02` §1 typed columns. */
export type WorkspaceConfig = {
  id: string;
  name: string;
  state: string;
  tz: string | null;
  posts_per_day: number | null;
  posting_hours_start: number | null;
  posting_hours_end: number | null;
  approval_mode: string | null;
  auto_reapprove_returning: boolean | null;
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
  /** When an offboarding workspace can last be restored; server-computed (#1127). */
  restorable_until: string | null;
  /** The deployment's fallbacks for the columns `053` declares NULL, served
   *  rather than retyped here — the settings cards show them and must not
   *  hold a copy (#1366; the same reason as `restorable_until`). */
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
};

export type SourcesResponse = { sources: SourceRow[] };

/**
 * `GET /workspaces/{ws}/drive` — the WORKSPACE's Google Drive grant (069,
 * `07` §15: one per workspace, every folder under it), projected as the
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

/** What a history tab means: the terminal outcomes. #1044 names this set. */
export const HISTORY_STATES = "posted,skipped,rejected";

/**
 * What a queue means: everything before a terminal outcome — all seven.
 *
 * #1044 classifies `queue-detail` as `intents?state=` without naming the set,
 * so this list is MINE. The previous version stated that rule and then broke
 * it: `publishing`/`publishing_ambiguous` were excluded by name and
 * `review_required` silently, so "In Queue" undercounted by exactly the number
 * of stuck intents — a plain label, a confident number, no footnote. A card
 * that undercounts without disclosing is worse than one that errors, because
 * nothing on the page invites the reader to doubt it.
 *
 * The gap is now ABSENT rather than merely documented: definition and contents
 * agree, and `intent-states-contract.test.ts` reads the API's own
 * `INTENT_STATES` and fails unless QUEUE and TERMINAL together account for
 * every member. Documenting an exclusion only helps a reader of THIS file; the
 * undercount was read on the dashboard.
 *
 * The distinction that `publishing` is "in flight rather than queued" is real,
 * but it is not one the label "In Queue" draws for a reader, and holding it
 * cost a wrong number. If the queue should ever exclude a non-terminal state
 * again, the contract test makes that a deliberate, visible edit.
 *
 * DERIVED, NOT RE-TYPED (TD-D1). The members are `NON_TERMINAL_STATES` in
 * `intents.ts`; this is the `?state=` spelling of them. A state added there
 * now lands here without an edit, which is the failure mode the paragraph
 * above describes, closed at the source rather than watched for.
 */
export const QUEUE_STATES = NON_TERMINAL_STATES.join(",");

/**
 * The terminal outcomes — the other half of the partition. Migration `055`
 * labels these `TERMINAL` in `ck_intent_state` itself, so this is the schema's
 * classification rather than one invented here.
 *
 * Deliberately a SUPERSET of `HISTORY_STATES`: `expired`, `failed` and
 * `cancelled` are terminal but are not shown on the history tab, which is
 * #1044's call and not this file's. Naming them here is what lets the contract
 * test account for all thirteen states rather than for thirteen minus whatever
 * the history tab happens to render.
 *
 * DERIVED from `intents.TERMINAL_STATES`, which mirrors
 * `command_executors.TERMINAL_STATES` (Python). The superset relationship
 * with `HISTORY_STATES` above is unchanged and still this tier's call.
 */
export const TERMINAL_STATES = TERMINAL_STATE_LIST.join(",");

/**
 * The one queue member an operator has to act on personally.
 *
 * Named because the calendar surfaces it separately when non-zero: it is
 * reached via the G5 poison ladder once publish retries exhaust, it is
 * operator-owned, and it does not clear itself. Counting it is necessary but
 * not sufficient — a stuck intent folded anonymously into a queue depth is
 * accurate and still tells nobody to go and look at it.
 */
export const REVIEW_REQUIRED_STATE = "review_required";

/** The schedule strip: only what has a slot. #1044 names this one. */
export const SCHEDULED_STATES = "scheduled";

// ── Derived views ──────────────────────────────────────────────────────────

/**
 * A figure no target route serves. NEVER rendered as a number.
 *
 * Greppable on purpose: `grep -rn UNAVAILABLE landing/src/` lists every place
 * the screens are currently degraded, and that list should shrink to nothing
 * when #1048 is ruled on.
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
   * rule as #1048's columns, different cause: this one resolves itself the
   * moment one publish is attempted.
   */
  success_rate: number | Unavailable;
  /** `Unavailable` when there is no window to average over — see above. */
  avg_per_day: number | Unavailable;
};

export type CategoryView = {
  category: string;
  posted: number;
  total: number;
  actual_ratio: number;
  /** #1048: the configured mix has no target-side source. */
  configured_ratio: number | Unavailable;
};

export type PoolHealthView = {
  total_active: number;
  never_posted: number;
  by_category: { name: string; count: number }[];
  /** #1048: `times_posted` buckets have no target-side source. */
  posted_once: number | Unavailable;
  /** #1048: `times_posted` buckets have no target-side source. */
  posted_multiple: number | Unavailable;
  /**
   * Eligible to post now — available, and past its repost TTL.
   *
   * NOT in #1048's original two, found while building this: `stats` serves
   * `media_by_state`, which is a different axis, and the TTL lives on the
   * workspace config rather than the item. Same class as the other two and
   * added to that issue rather than quietly defaulted here.
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
    // Publish outcomes only. The legacy card lumped Instagram publishes with
    // Telegram deliveries and reported 1% after a delivery burst (#466/#467);
    // `intents_by_state` cannot make that mistake because Telegram delivery is
    // not an intent state — there is nothing of the other kind in the divisor.
    success_rate: attempts === 0 ? null : posted / attempts,
    // From the cap ledger, not from a bounded list. The previous version said
    // in as many words that "zero days means no window rather than a zero
    // average" — and then returned 0 anyway, which is the fabrication it had
    // just named. No window is `Unavailable`, like the rate above.
    avg_per_day: days === 0 ? null : postedOverWindow / days,
  };
}

/** The category table, joined from the two count dicts. */
export function deriveCategories(stats: StatsResponse): CategoryView[] {
  const totals = stats.media_by_category ?? {};
  const posted = stats.posted_by_category ?? {};
  const postedOverall = sum(posted);
  return Object.keys({ ...totals, ...posted })
    .filter((c) => c !== "")
    .sort()
    .map((category) => ({
      category,
      posted: posted[category] ?? 0,
      total: totals[category] ?? 0,
      actual_ratio:
        postedOverall === 0 ? 0 : (posted[category] ?? 0) / postedOverall,
      configured_ratio: null,
    }));
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
 * What the Settings screen can HONESTLY show from the routes that exist (#1063).
 *
 * Settings asked `init` for everything. There is no `init` route and there is
 * no plan for one, so the screen rendered `RouterUnavailable` on every load —
 * taking the Accounts and Integrations tabs with it.
 *
 * ── Why this is a derivation and not four reads spread across the page ──────
 *
 * The old page coalesced every field (`setup.X ?? default`). That is the
 * dangerous half, and it is NOT what the hard bail protects against: the bail
 * catches a fetch that FAILED, while a defaulted field arrives inside a
 * response that SUCCEEDED. `media_sync_enabled ?? false` renders "Auto-sync
 * disabled" — a claim about the workspace manufactured from a missing column,
 * which is the exact harm `settings/page.tsx` warns about in its own comment.
 *
 * So every field is resolved in one place, and a field with no source is
 * `Unavailable` — never a default. The type is non-optional `null` for the
 * reason #1051 gave for `configured_ratio`: an optional field invites `?? 0` at
 * the consumer, while a non-optional null makes the compiler stop at every
 * consumer until it says what it shows instead. Sourcing one later is then a
 * VISIBLE type change rather than a silent behaviour change.
 */
export type SettingsView = {
  // ── From the workspace row (`GET /workspaces/{ws}`) ──────────────────────
  posts_per_day: number | null;
  posting_hours_start: number | null;
  posting_hours_end: number | null;
  /** IANA zone the window is read in (`workspaces.tz`, `059` `fn_next_slot`). */
  tz: string | null;
  is_paused: boolean | null;
  dry_run_mode: boolean | null;
  /** The workspace row calls this `api_publishing_enabled`. Renamed once, here. */
  enable_instagram_api: boolean | null;
  enable_ai_captions: boolean | null;
  repost_ttl_days: number | null;
  skip_ttl_days: number | null;
  /** Carried but NOT rendered: no code in the tier reads `caption_style`, so
   *  its card is not shown (#1366). Kept so the card can return unchanged the
   *  day `prompts.render_card` honours it. */
  caption_style: string | null;
  /** The deployment's fallbacks for the two NULL-on-purpose TTL columns.
   *  Served, never retyped here — a frontend copy is what #1366 was. */
  defaults: { repost_ttl_days: number; skip_ttl_days: number };

  // ── From `sources` and `stats` ───────────────────────────────────────────
  gdrive_connected: boolean;
  media_source_type: string | null;
  media_source_state: string | null;
  media_count: number;

  // ── NO SOURCE ON THE TARGET TIER. `Unavailable`, never a default. ────────
  //
  // Greppable on purpose (see `Unavailable`): this list is the Settings half of
  // what #1063 option 2 has to supply, and it should shrink to nothing.
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
    enable_ai_captions: config.enable_ai_captions,
    repost_ttl_days: config.repost_ttl_days,
    skip_ttl_days: config.skip_ttl_days,
    caption_style: config.caption_style,
    defaults: config.defaults,

    /*
     * #1081 made this the CREDENTIAL's answer rather than the source row's
     * (a folder pasted by link existed before any grant did). Since 069
     * (#1165) the credential is the WORKSPACE's — one grant, every folder
     * under it — so the answer comes from `GET /workspaces/{ws}/drive`, and a
     * source row says nothing about it either way.
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

// The target returns `handle`/`state`, not `instagram_username`/`is_active`.
// Typed as `InstagramAccount` this compiled and rendered a bare `@` for every
// row — the type asserted a shape nothing produced (#1048's class, #1089).
export type AccountsResponse = { accounts?: import("./types").Destination[] };

// The `init`/`SetupState` pair that used to close this file is deleted
// (TD-D4). It carried a paragraph saying the settings screen "stays on `init`
// and answers `RouterUnavailable`"; `deriveSettings` above had already made
// that false, and neither type had an importer.
