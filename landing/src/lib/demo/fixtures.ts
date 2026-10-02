import type { CategoryMixResponse } from "@/lib/category-mix";
import type {
  AccountsResponse,
  SourcesResponse,
  StatsResponse,
  WorkspaceConfig,
} from "@/lib/dashboard-payloads";
import type { Intent, IntentState } from "@/lib/intents";

/**
 * The sample workspace `/demo` shows a signed-out visitor (#1480).
 *
 * EVERY VALUE IS A PLACEHOLDER: Example Co, the account example.brand, three
 * generic folders and generic file names. No business, handle, caption,
 * media or customer, and nothing copied from production. The page is public
 * and so is this file.
 *
 * TYPED AS THE ROUTES' OWN RESPONSES, and drawn through the dashboard's own
 * derivations and components, so a dashboard shape this sample no longer
 * fits fails `tsc` here rather than drifting quietly.
 *
 * BUILT FROM `now`, so the sample is always current. A story is put to its
 * workspace when its slot arrives (`fn_prompts_due`), so the six waiting on a
 * decision have slots in the last few hours; three more are due later; a
 * month of history sits behind them. The workspace is all clear: no
 * condition is raised, because a raised condition links into `/dashboard`.
 */

export type SampleWorkspace = {
  config: WorkspaceConfig;
  stats: StatsResponse;
  accounts: AccountsResponse;
  sources: SourcesResponse;
  mix: CategoryMixResponse;
  /** What has not finished, in slot order: the Queue. */
  queue: Intent[];
  /** What finished, newest first: recent activity, and the calendar's past. */
  history: Intent[];
};

const MINUTE = 60_000;
const HOUR = 60 * MINUTE;
const DAY = 24 * HOUR;

const ACCOUNT = {
  id: "sample-account",
  handle: "example.brand",
  displayName: "Example Co",
};

/** The three Drive folders and the share of posts the mix plans for each. */
const FOLDERS = [
  { id: "sample-folder-products", name: "Product shots", ratio: 0.5, effective: 50, media: 64 },
  { id: "sample-folder-studio", name: "Behind the scenes", ratio: 0.3, effective: 30, media: 49 },
  { id: "sample-folder-memes", name: "Memes", ratio: 0.2, effective: 20, media: 29 },
] as const;

type Folder = 0 | 1 | 2;

type StoryInput = { file: string; folder: Folder; video?: boolean };

/** Waiting on a decision; their slots arrived in the last few hours, oldest first. */
const WAITING: StoryInput[] = [
  { file: "new-arrivals-flat-lay.jpg", folder: 0 },
  { file: "packing-orders.mp4", folder: 1, video: true },
  { file: "monday-mood.jpg", folder: 2 },
  { file: "studio-desk.jpg", folder: 1 },
  { file: "spring-colours.jpg", folder: 0 },
  { file: "when-the-printer-works.jpg", folder: 2 },
];

/** Planned for later; nothing to decide yet. */
const SCHEDULED: StoryInput[] = [
  { file: "close-up-texture.jpg", folder: 0 },
  { file: "team-lunch.jpg", folder: 1 },
  { file: "weekend-plans.jpg", folder: 2 },
];

/** What finished, newest first: when, in days and hours ago, and how it ended. */
const FINISHED: (StoryInput & { state: IntentState; ago: number })[] = [
  { file: "product-lineup.jpg", folder: 0, state: "posted", ago: 1 * DAY + 2 * HOUR },
  { file: "coffee-first.jpg", folder: 2, state: "posted", ago: 1 * DAY + 6 * HOUR },
  { file: "half-finished-sketch.jpg", folder: 1, state: "skipped", ago: 2 * DAY + 1 * HOUR },
  { file: "gift-wrap-station.jpg", folder: 1, state: "posted", ago: 2 * DAY + 5 * HOUR },
  { file: "colour-swatches.jpg", folder: 0, state: "posted", ago: 3 * DAY + 3 * HOUR },
  { file: "blurry-test-shot.jpg", folder: 0, state: "rejected", ago: 4 * DAY + 2 * HOUR },
  { file: "friday-feeling.jpg", folder: 2, state: "posted", ago: 4 * DAY + 7 * HOUR },
  { file: "window-display.jpg", folder: 0, state: "posted", ago: 5 * DAY + 4 * HOUR },
  { file: "label-printing.mp4", folder: 1, state: "posted", ago: 6 * DAY + 2 * HOUR, video: true },
  { file: "plot-twist.jpg", folder: 2, state: "posted", ago: 6 * DAY + 8 * HOUR },
];

/** Posts per day over the stats window, oldest first; the cap is `posts_per_day`. */
const DAILY_POSTS = [4, 5, 3, 6, 5, 2, 4];
const POSTS_PER_DAY = 6;
const WINDOW_DAYS = 30;

function story(
  id: string,
  input: StoryInput,
  state: IntentState,
  slot: Date,
  entered: Date,
): Intent {
  return {
    id,
    state,
    ig_account_id: ACCOUNT.id,
    media_item_id: `${id}-media`,
    schedule_slot_at: slot.toISOString(),
    approval_mode: "manual",
    published_via: state === "posted" ? "api" : null,
    publish_step: null,
    cancel_requested: false,
    ig_permalink: null,
    entered_state_at: entered.toISOString(),
    created_at: new Date(slot.getTime() - DAY).toISOString(),
    file_name: input.file,
    media_kind: input.video ? "video" : "image",
    thumbnail_url: null,
    caption: null,
    category: FOLDERS[input.folder].name,
    account_handle: ACCOUNT.handle,
    account_display_name: ACCOUNT.displayName,
  };
}

/** `YYYY-MM-DD`, the shape `posts_by_day.local_date` carries. */
const isoDate = (d: Date) => d.toISOString().slice(0, 10);

export function sampleWorkspace(now: Date): SampleWorkspace {
  const t = now.getTime();
  const hour = Math.floor(t / HOUR) * HOUR;
  const longAgo = new Date(t - 60 * DAY).toISOString();

  const queue = [
    ...WAITING.map((input, i) => {
      const slot = new Date(hour - (WAITING.length - 1 - i) * HOUR);
      return story(`sample-waiting-${i + 1}`, input, "awaiting_approval", slot, new Date(slot.getTime() + MINUTE));
    }),
    ...SCHEDULED.map((input, i) => {
      const slot = new Date(hour + (i + 1) * 3 * HOUR);
      return story(`sample-scheduled-${i + 1}`, input, "scheduled", slot, new Date(slot.getTime() - DAY));
    }),
  ];

  const history = FINISHED.map((input, i) => {
    const entered = new Date(t - input.ago);
    return story(`sample-finished-${i + 1}`, input, input.state, new Date(entered.getTime() - 5 * MINUTE), entered);
  });

  const postsByDay = Array.from({ length: WINDOW_DAYS }, (_, i) => ({
    local_date: isoDate(new Date(t - (WINDOW_DAYS - 1 - i) * DAY)),
    count: DAILY_POSTS[i % DAILY_POSTS.length],
    cap: POSTS_PER_DAY,
  }));
  const posted = postsByDay.reduce((a, d) => a + d.count, 0);

  // Each folder's posts in the window, in its planned share; the last folder
  // takes the remainder so the three add up to the posted count exactly.
  const products = Math.round(posted * FOLDERS[0].ratio);
  const studio = Math.round(posted * FOLDERS[1].ratio);
  const postedBySource = {
    [FOLDERS[0].id]: products,
    [FOLDERS[1].id]: studio,
    [FOLDERS[2].id]: posted - products - studio,
  };

  return {
    config: {
      id: "sample-workspace",
      name: ACCOUNT.displayName,
      state: "active",
      tz: "America/New_York",
      posts_per_day: POSTS_PER_DAY,
      posting_hours_start: 9,
      posting_hours_end: 21,
      approval_mode: "manual",
      auto_reapprove_returning: false,
      approval_ttl_minutes: null,
      dry_run_mode: false,
      is_paused: false,
      paused_at: null,
      repost_ttl_days: null,
      skip_ttl_days: null,
      caption_style: null,
      enable_ai_captions: false,
      api_publishing_enabled: true,
      offboarding_at: null,
      restorable_until: null,
      defaults: { repost_ttl_days: 30, skip_ttl_days: 7 },
      created_at: longAgo,
      updated_at: null,
    },
    stats: {
      intents_by_state: {
        posted,
        skipped: 11,
        rejected: 4,
        failed: 1,
        awaiting_approval: WAITING.length,
        scheduled: SCHEDULED.length,
      },
      media_by_state: { available: FOLDERS.reduce((a, f) => a + f.media, 0) },
      media_never_posted: 37,
      media_by_category: Object.fromEntries(FOLDERS.map((f) => [f.name, f.media])),
      posted_by_source: postedBySource,
      posts_by_day: postsByDay,
      accounts: 1,
      sources: FOLDERS.length,
    },
    accounts: {
      accounts: [
        {
          id: ACCOUNT.id,
          provider_account_ref: "sample-instagram-account",
          handle: ACCOUNT.handle,
          display_name: ACCOUNT.displayName,
          state: "active",
          next_slot_at: queue[WAITING.length].schedule_slot_at,
          last_posted_at: history[0].entered_state_at,
          credential_status: "active",
          credential_connected_at: longAgo,
        },
      ],
    },
    sources: {
      sources: FOLDERS.map((f) => ({
        id: f.id,
        provider: "gdrive",
        state: "active",
        next_sync_at: new Date(t + HOUR).toISOString(),
        last_sync_success_at: new Date(t - 20 * MINUTE).toISOString(),
        alerted_at: null,
        created_at: longAgo,
        folder_ref: `${f.id}-drive`,
        folder_name: f.name,
        removed: false,
      })),
    },
    mix: {
      rows: FOLDERS.map((f) => ({
        source_id: f.id,
        provider: "gdrive",
        name: f.name,
        state: "active",
        media_count: f.media,
        ratio: f.ratio,
        effective: f.effective,
      })),
    },
    queue,
    history,
  };
}
