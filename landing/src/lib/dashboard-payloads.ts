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

/** A row of `GET …/intents?state=&limit=` — `_INTENT_COLUMNS`, joined to media. */
export type IntentRow = {
  id: string;
  state: string;
  ig_account_id: string | null;
  media_item_id: string;
  schedule_slot_at: string | null;
  approval_mode: string | null;
  published_via: string | null;
  publish_step: string | null;
  cancel_requested: boolean;
  ig_permalink: string | null;
  entered_state_at: string;
  created_at: string;
  file_name: string;
  media_kind: string;
  thumbnail_url: string | null;
  caption: string | null;
  category: string | null;
};

export type IntentsResponse = { intents: IntentRow[]; limit: number };

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
  /** #1078. `none` = never connected; `expired`/`revoked` = reconnect needed. */
  credential_status: "none" | "active" | "expired" | "revoked";
  credential_connected_at: string | null;
};

export type SourcesResponse = { sources: SourceRow[] };

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
 */
export const QUEUE_STATES =
  "scheduled,prompt_pending,awaiting_approval,approved," +
  "publishing,publishing_ambiguous,review_required";

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
 */
export const TERMINAL_STATES =
  "posted,skipped,rejected,expired,failed,cancelled";

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
  is_paused: boolean | null;
  dry_run_mode: boolean | null;
  /** The workspace row calls this `api_publishing_enabled`. Renamed once, here. */
  enable_instagram_api: boolean | null;
  enable_ai_captions: boolean | null;
  repost_ttl_days: number | null;
  skip_ttl_days: number | null;
  caption_style: string | null;

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
): SettingsView {
  // The first Drive source, whatever its state. State is carried rather than
  // flattened into the boolean: "connected but erroring" and "not connected"
  // are different facts with different remedies.
  const drive = sources.find((s) => s.provider === "gdrive") ?? null;
  const byState = stats.media_by_state ?? {};

  return {
    posts_per_day: config.posts_per_day,
    posting_hours_start: config.posting_hours_start,
    posting_hours_end: config.posting_hours_end,
    is_paused: config.is_paused,
    dry_run_mode: config.dry_run_mode,
    enable_instagram_api: config.api_publishing_enabled,
    enable_ai_captions: config.enable_ai_captions,
    repost_ttl_days: config.repost_ttl_days,
    skip_ttl_days: config.skip_ttl_days,
    caption_style: config.caption_style,

    /*
     * #1081. This was `drive !== null` — CONNECTED because a source ROW
     * EXISTED, which is true the instant someone pastes a folder link and
     * before any credential is written. It said nothing about credentials at
     * all, and it survived because until #1078 the payload carried nothing
     * that could answer the question.
     *
     * `state` is NOT the replacement and was rejected separately: a source
     * with a dead credential flips to `error`, but a source created and never
     * credentialed is `active` too, so it separates broken from not-broken and
     * cannot separate connected from never-connected.
     *
     * Note what this keeps from the old behaviour deliberately: an ERRORING
     * source with a live credential still reads connected. That was the sound
     * half of the reasoning here — collapsing it to "not connected" sends
     * someone to reconnect a source that is already there. It now turns on the
     * credential rather than on the source row.
     *
     * BOUND, so no caller reads more confidence into this than it has: an
     * UNDECRYPTABLE credential reads `active`. The list query cannot know a
     * payload fails to decrypt without attempting decryption, so this means
     * "a usable-looking credential exists", not "a request will succeed"
     * (navi, #1080 review).
     */
    gdrive_connected: drive?.credential_status === "active",
    media_source_type: drive?.provider ?? null,
    media_source_state: drive?.state ?? null,
    media_count: Object.values(byState).reduce((a, n) => a + n, 0),

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

// ── STILL LEGACY: the settings screen ──────────────────────────────────────
//
// `init` is NOT repointed in this change and these types are kept for it. That
// is deliberate and is not the same gap as the two contested figures.
//
// Settings looked like the fourth rename and is not. Its READS have a home
// (`GET /workspaces/{ws}` serves posts_per_day, the posting window, the TTLs,
// caption style, dry-run and pause), but its WRITES are ten separate legacy
// endpoints — `schedule`, `toggle-setting`, `update-setting`,
// `update-string-setting`, `disconnect-gdrive`, `sync-media`,
// `switch-account`, `remove-account`, `category-mix`, `update-category-mix` —
// which have to be mapped onto the closed command vocabulary with idempotency
// keys, and two of them (`connect_account` / `reconnect_account`) are 501 on
// the target tier BY DESIGN, being browser redirect flows rather than commands.
//
// Repointing the reads alone would be worse than leaving it: the form would
// show real current values beside a save button that silently 404s, and
// someone would change a setting to match what they saw. So the screen stays
// on `init` and answers `RouterUnavailable` — unchanged by this PR, honest
// about being unreachable, and tracked separately.

export type SetupState = {
  onboarding_completed?: boolean;
  posts_per_day?: number;
  posting_hours_start?: number;
  posting_hours_end?: number;
  is_paused?: boolean;
  dry_run_mode?: boolean;
  enable_instagram_api?: boolean;
  show_verbose_notifications?: boolean;
  media_sync_enabled?: boolean;
  enable_ai_captions?: boolean;
  repost_ttl_days?: number | null;
  skip_ttl_days?: number | null;
  caption_style?: string | null;
  send_lifecycle_notifications?: boolean | null;
  gdrive_connected?: boolean;
  gdrive_email?: string | null;
  media_count?: number;
  media_source_type?: string | null;
  media_source_root?: string | null;
};

export type InitResponse = { setup_state?: SetupState };
