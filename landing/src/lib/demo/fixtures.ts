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
 * BUILT FROM `now`, on the workspace's own cadence: six posts a day, every
 * two hours from 9:00, in its own zone. A story is put to its workspace when
 * its slot arrives (`fn_prompts_due`), so the six waiting on a decision are
 * the six most recent slots; the next three are scheduled; the history sits
 * on the slots before them. The workspace is all clear: no condition is
 * raised, because a raised condition links into `/dashboard`.
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
const DAY = 24 * 60 * MINUTE;

/** The workspace's zone, and its posting hours there: 9:00 to 21:00, a post every two hours. */
export const SAMPLE_TZ = "America/New_York";
export const SAMPLE_POSTING_HOURS = [9, 11, 13, 15, 17, 19];
const POSTS_PER_DAY = SAMPLE_POSTING_HOURS.length;

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

/** Waiting on a decision, oldest first. */
const WAITING: StoryInput[] = [
  { file: "new-arrivals-flat-lay.jpg", folder: 0 },
  { file: "packing-orders.mp4", folder: 1, video: true },
  { file: "monday-mood.jpg", folder: 2 },
  { file: "studio-desk.jpg", folder: 1 },
  { file: "spring-colours.jpg", folder: 0 },
  { file: "when-the-printer-works.jpg", folder: 2 },
];

/** Planned for the next slots; nothing to decide yet. */
const SCHEDULED: StoryInput[] = [
  { file: "close-up-texture.jpg", folder: 0 },
  { file: "team-lunch.jpg", folder: 1 },
  { file: "weekend-plans.jpg", folder: 2 },
];

/** What finished, newest first, and how it ended. */
const FINISHED: (StoryInput & { state: IntentState })[] = [
  { file: "product-lineup.jpg", folder: 0, state: "posted" },
  { file: "coffee-first.jpg", folder: 2, state: "posted" },
  { file: "half-finished-sketch.jpg", folder: 1, state: "skipped" },
  { file: "gift-wrap-station.jpg", folder: 1, state: "posted" },
  { file: "colour-swatches.jpg", folder: 0, state: "posted" },
  { file: "blurry-test-shot.jpg", folder: 0, state: "rejected" },
  { file: "friday-feeling.jpg", folder: 2, state: "posted" },
  { file: "window-display.jpg", folder: 0, state: "posted" },
  { file: "label-printing.mp4", folder: 1, state: "posted", video: true },
  { file: "plot-twist.jpg", folder: 2, state: "posted" },
];

/** Every third slot before the waiting ones: about two finished stories a day. */
const FINISHED_EVERY = 3;

/** Posts per day over the stats window, oldest first; the cap is `posts_per_day`. */
const DAILY_POSTS = [4, 5, 3, 6, 5, 2, 4];
const WINDOW_DAYS = 30;

type LocalDate = { year: number; month: number; day: number };

/** An instant's wall clock in a zone. */
function wallClock(instant: number, timeZone: string) {
  const parts = new Intl.DateTimeFormat("en-US", {
    timeZone,
    hourCycle: "h23",
    year: "numeric",
    month: "numeric",
    day: "numeric",
    hour: "numeric",
    minute: "numeric",
    second: "numeric",
  }).formatToParts(new Date(instant));
  const part = (type: Intl.DateTimeFormatPartTypes) =>
    Number(parts.find((p) => p.type === type)?.value);
  return {
    year: part("year"),
    month: part("month"),
    day: part("day"),
    hour: part("hour"),
    minute: part("minute"),
    second: part("second"),
  };
}

/** How far the zone's wall clock runs ahead of UTC at an instant, in milliseconds. */
function zoneOffset(instant: number, timeZone: string): number {
  const w = wallClock(instant, timeZone);
  return Date.UTC(w.year, w.month - 1, w.day, w.hour, w.minute, w.second) - instant;
}

/** The instant a zone's wall clock reads `hour`:00 on a date. */
function wallTime({ year, month, day }: LocalDate, hour: number, timeZone: string): number {
  const asUtc = Date.UTC(year, month - 1, day, hour);
  const first = asUtc - zoneOffset(asUtc, timeZone);
  // Once more at the guess, so a date that crosses a clock change lands right.
  return asUtc - zoneOffset(first, timeZone);
}

/** A date `days` after `date`, counted on the calendar. */
function addDays({ year, month, day }: LocalDate, days: number): LocalDate {
  const d = new Date(Date.UTC(year, month - 1, day + days));
  return { year: d.getUTCFullYear(), month: d.getUTCMonth() + 1, day: d.getUTCDate() };
}

/** `YYYY-MM-DD`, the shape `posts_by_day.local_date` carries. */
const isoDate = ({ year, month, day }: LocalDate) =>
  `${year}-${String(month).padStart(2, "0")}-${String(day).padStart(2, "0")}`;

/** Every posting slot from `before` days back to two days ahead of `now`, in order. */
function slotsAround(now: number, before: number): number[] {
  const today = wallClock(now, SAMPLE_TZ);
  const slots: number[] = [];
  for (let offset = -before; offset <= 2; offset++) {
    const date = addDays(today, offset);
    for (const hour of SAMPLE_POSTING_HOURS) slots.push(wallTime(date, hour, SAMPLE_TZ));
  }
  return slots.sort((a, b) => a - b);
}

function story(
  id: string,
  input: StoryInput,
  state: IntentState,
  slot: number,
  entered: number,
): Intent {
  return {
    id,
    state,
    ig_account_id: ACCOUNT.id,
    media_item_id: `${id}-media`,
    schedule_slot_at: new Date(slot).toISOString(),
    approval_mode: "manual",
    published_via: state === "posted" ? "api" : null,
    publish_step: null,
    cancel_requested: false,
    ig_permalink: null,
    entered_state_at: new Date(entered).toISOString(),
    created_at: new Date(slot - DAY).toISOString(),
    file_name: input.file,
    media_kind: input.video ? "video" : "image",
    thumbnail_url: null,
    caption: null,
    category: FOLDERS[input.folder].name,
    account_handle: ACCOUNT.handle,
    account_display_name: ACCOUNT.displayName,
  };
}

export function sampleWorkspace(now: Date): SampleWorkspace {
  const t = now.getTime();
  const longAgo = new Date(t - 60 * DAY).toISOString();

  const daysBack = Math.ceil((FINISHED.length * FINISHED_EVERY + WAITING.length) / POSTS_PER_DAY) + 1;
  const slots = slotsAround(t, daysBack);
  const arrived = slots.filter((s) => s <= t);
  const waitingSlots = arrived.slice(-WAITING.length);
  const earlier = arrived.slice(0, -WAITING.length).reverse();
  const nextSlots = slots.filter((s) => s > t).slice(0, SCHEDULED.length);

  const queue = [
    ...WAITING.map((input, i) =>
      story(`sample-waiting-${i + 1}`, input, "awaiting_approval", waitingSlots[i], waitingSlots[i] + MINUTE),
    ),
    ...SCHEDULED.map((input, i) =>
      story(`sample-scheduled-${i + 1}`, input, "scheduled", nextSlots[i], nextSlots[i] - DAY),
    ),
  ];

  const history = FINISHED.map((input, i) => {
    const slot = earlier[i * FINISHED_EVERY + 1];
    return story(`sample-finished-${i + 1}`, input, input.state, slot, slot + 2 * MINUTE);
  });

  const today = wallClock(t, SAMPLE_TZ);
  const postsByDay = Array.from({ length: WINDOW_DAYS }, (_, i) => ({
    local_date: isoDate(addDays(today, i - (WINDOW_DAYS - 1))),
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
      tz: SAMPLE_TZ,
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
        next_sync_at: new Date(t + 60 * MINUTE).toISOString(),
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
