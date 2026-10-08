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
 * two hours from 9:00, in its own zone, the one the landing's cards show. A
 * story is put to its workspace when its slot arrives (`fn_prompts_due`), so
 * the six waiting for a tap are the six most recent slots, and the next three
 * are scheduled.
 *
 * ONE SET OF STORIES BEHIND EVERY SCREEN. Every slot of the stats window
 * before the waiting six has a finished story, and the Overview's counts,
 * chart and mix are counted from those stories, so the Overview, the Queue and
 * the Calendar agree about what happened when.
 *
 * DIRECT POSTING IS OFF, as in a new workspace: a story is posted by hand and
 * recorded with Posted myself. The workspace is all clear: no condition is
 * raised, because a raised condition links into `/dashboard`.
 */

/**
 * A finished story, as the screens that list them read it: the Overview's
 * Recent activity and the Calendar's past. Narrowed from the intent row, so
 * the page carries a month of them without every column.
 */
export type FinishedStory = Pick<
  Intent,
  "id" | "state" | "file_name" | "category" | "schedule_slot_at" | "entered_state_at"
>;

export type SampleWorkspace = {
  config: WorkspaceConfig;
  stats: StatsResponse;
  accounts: AccountsResponse;
  sources: SourcesResponse;
  mix: CategoryMixResponse;
  /** What has not finished, in slot order: the Queue. */
  queue: Intent[];
  /** What finished in the stats window, newest first: Recent activity, and the Calendar's past. */
  history: FinishedStory[];
};

const MINUTE = 60_000;
const DAY = 24 * 60 * MINUTE;

/** The workspace's zone, and its posting hours there: 9:00 to 21:00, a post every two hours. */
export const SAMPLE_TZ = "Europe/London";
/** The zone as a person names it, for a line that says which clock the times are on. */
export const SAMPLE_ZONE_NAME = "London time";
export const SAMPLE_POSTING_HOURS = [9, 11, 13, 15, 17, 19];
const POSTS_PER_DAY = SAMPLE_POSTING_HOURS.length;

/** Direct posting, off as in a new workspace (`api_publishing_enabled` defaults to false). */
export const SAMPLE_API_PUBLISHING = false;

const ACCOUNT = {
  id: "sample-account",
  handle: "example.brand",
  displayName: "Example Co",
};

/**
 * The three Drive folders and the share of posts the mix plans for each. Each
 * holds more items than a month of its posts, so none repeats inside the
 * 30-day repost window.
 */
const FOLDERS = [
  { id: "sample-folder-products", name: "Product shots", ratio: 0.5, effective: 50, media: 120 },
  { id: "sample-folder-studio", name: "Behind the scenes", ratio: 0.3, effective: 30, media: 80 },
  { id: "sample-folder-memes", name: "Memes", ratio: 0.2, effective: 20, media: 60 },
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

/** The names a finished story's file takes in each folder; a number keeps each file apart. */
const FINISHED_NAMES: Record<Folder, readonly string[]> = {
  0: ["product-lineup", "colour-swatches", "window-display", "gift-box-detail", "fabric-close-up"],
  1: ["gift-wrap-station", "half-finished-sketch", "morning-setup", "packing-table", "label-printing"],
  2: ["coffee-first", "friday-feeling", "plot-twist", "mood-board", "inbox-zero"],
};

/** The folder of each slot in a run of ten: five, three and two, the mix's shares. */
const FOLDER_RUN: readonly Folder[] = [0, 1, 0, 2, 0, 1, 0, 2, 0, 1];

/** The stats window, in days, today included. */
const WINDOW_DAYS = 30;

type LocalDate = { year: number; month: number; day: number };

/** One formatter per zone: a month of slots reads the clock several hundred times a render. */
const clocks = new Map<string, Intl.DateTimeFormat>();

/** An instant's wall clock in a zone. */
function wallClock(instant: number, timeZone: string) {
  let clock = clocks.get(timeZone);
  if (!clock) {
    clock = new Intl.DateTimeFormat("en-US", {
      timeZone,
      hourCycle: "h23",
      year: "numeric",
      month: "numeric",
      day: "numeric",
      hour: "numeric",
      minute: "numeric",
      second: "numeric",
    });
    clocks.set(timeZone, clock);
  }
  const parts = clock.formatToParts(new Date(instant));
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

/** A slot's place in the workspace's run of slots: the same at every render, so a slot keeps its story. */
function ordinalOf(slot: number): number {
  const w = wallClock(slot, SAMPLE_TZ);
  return (Date.UTC(w.year, w.month - 1, w.day) / DAY) * POSTS_PER_DAY + SAMPLE_POSTING_HOURS.indexOf(w.hour);
}

/** How a finished story ended: most posted; one slot in eleven was skipped, one in twenty-nine rejected. */
function endingOf(ordinal: number): IntentState {
  if (ordinal % 29 === 11) return "rejected";
  if (ordinal % 11 === 4) return "skipped";
  return "posted";
}

/** The story that finished in an earlier slot: its folder from the mix, its file and its ending from its place in the run. */
function finishedStory(slot: number): FinishedStory {
  const ordinal = ordinalOf(slot);
  const folder = FOLDER_RUN[ordinal % FOLDER_RUN.length];
  const names = FINISHED_NAMES[folder];
  const number = String(Math.floor(ordinal / FOLDER_RUN.length) % 1000).padStart(3, "0");
  return {
    id: `sample-finished-${ordinal}`,
    state: endingOf(ordinal),
    file_name: `${names[ordinal % names.length]}-${number}.jpg`,
    category: FOLDERS[folder].name,
    schedule_slot_at: new Date(slot).toISOString(),
    entered_state_at: new Date(slot + 2 * MINUTE).toISOString(),
  };
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
    // The sample's stories are its slot plan's; none was planned by a person.
    origin: "cadence",
    scheduled_by_user_id: null,
    scheduled_by: null,
    tz: SAMPLE_TZ,
    miss_reason: null,
    link_url: null,
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
  const today = wallClock(t, SAMPLE_TZ);
  const localDate = (instant: number) => isoDate(wallClock(instant, SAMPLE_TZ));
  const dates = Array.from({ length: WINDOW_DAYS }, (_, i) =>
    isoDate(addDays(today, i - (WINDOW_DAYS - 1))),
  );

  const slots = slotsAround(t, WINDOW_DAYS);
  const arrived = slots.filter((s) => s <= t);
  const waitingSlots = arrived.slice(-WAITING.length);
  const nextSlots = slots.filter((s) => s > t).slice(0, SCHEDULED.length);

  const queue = [
    ...WAITING.map((input, i) =>
      story(`sample-waiting-${i + 1}`, input, "awaiting_approval", waitingSlots[i], waitingSlots[i] + MINUTE),
    ),
    ...SCHEDULED.map((input, i) =>
      story(`sample-scheduled-${i + 1}`, input, "scheduled", nextSlots[i], nextSlots[i] - DAY),
    ),
  ];

  // Every slot of the window before the waiting ones finished. Newest first.
  const history = arrived
    .slice(0, -WAITING.length)
    .filter((slot) => localDate(slot) >= dates[0])
    .map(finishedStory)
    .reverse();

  // The Overview's figures are counted from those stories, so its chart, its
  // cards and Recent activity agree with the Queue and the Calendar.
  const posts = history.filter((s) => s.state === "posted");
  const ended = (state: IntentState) => history.filter((s) => s.state === state).length;
  const postsOn = new Map<string, number>();
  for (const s of posts) {
    const date = localDate(Date.parse(s.entered_state_at));
    postsOn.set(date, (postsOn.get(date) ?? 0) + 1);
  }
  const postsByDay = dates.map((date) => ({
    local_date: date,
    count: postsOn.get(date) ?? 0,
    cap: POSTS_PER_DAY,
  }));
  const postedBySource = Object.fromEntries(
    FOLDERS.map((f) => [f.id, posts.filter((s) => s.category === f.name).length]),
  );

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
      api_publishing_enabled: SAMPLE_API_PUBLISHING,
      offboarding_at: null,
      restorable_until: null,
      defaults: { repost_ttl_days: 30, skip_ttl_days: 7 },
      created_at: longAgo,
      updated_at: null,
    },
    stats: {
      intents_by_state: {
        posted: posts.length,
        skipped: ended("skipped"),
        rejected: ended("rejected"),
        // Posted by hand: no publish call is ever made, so none fails.
        failed: 0,
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
          last_posted_at: posts[0].entered_state_at,
          credential_status: "active",
          credential_connected_at: longAgo,
          tz: null,
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
