/**
 * The shapes the dashboard screens read from the target router.
 *
 * These were never typed. `backendFetchJson` returned `res.json()`, which is
 * `any`, so every dashboard page has been reading fields off an untyped object
 * and every `data?.field ?? 0` was unchecked — a renamed field on the backend
 * would have produced a zero, not an error.
 *
 * They are declared HERE rather than exported from the components because a
 * payload is a contract with the router, not a property of the chart that draws
 * it. When astrid's routes land these are what to check them against, and a
 * mismatch will be a type error rather than a silent zero.
 *
 * PROVISIONAL. Derived from what the components consume today, which is
 * evidence of what the legacy backend sent, not a specification of what the
 * target router will send. Expect to correct them against the real routes.
 */

export type AnalyticsSummary = {
  total_posts: number;
  posted: number;
  skipped: number;
  rejected: number;
  failed: number;
  success_rate: number;
  avg_per_day: number;
  ig_posted?: number;
  ig_failed?: number;
  ig_success_rate?: number;
  telegram_skipped?: number;
  telegram_failed?: number;
};

export type DailyCount = {
  date: string;
  posted: number;
  skipped: number;
  rejected: number;
  failed?: number;
};

export type CategoryData = {
  category: string;
  posted: number;
  total: number;
  success_rate: number;
  actual_ratio: number;
  configured_ratio: number;
};

export type HistoryItem = {
  posted_at: string;
  media_name: string;
  category: string;
  status: string;
  posting_method: string;
};

export type QueueItem = {
  scheduled_for: string;
  media_name: string;
  category: string;
  status: string;
};

export type ScheduleSlot = {
  slot_time: string;
  predicted_category: string | null;
};

export type PoolHealth = {
  total_active: number;
  never_posted: number;
  posted_once: number;
  posted_multiple: number;
  eligible_for_posting: number;
  by_category: { name: string; count: number }[];
};

export type MediaItem = {
  id: string;
  file_name: string;
  category: string;
  mime_type: string;
  file_size: number;
  times_posted: number;
  last_posted_at: string | null;
  source_type: string;
  has_thumbnail: boolean;
  created_at: string;
};

export type MediaLibraryResponse = {
  items: MediaItem[];
  total: number;
  page: number;
  page_size: number;
  categories: string[];
  pool_health: PoolHealth;
};

export type AnalyticsResponse = {
  summary?: AnalyticsSummary;
  daily_counts?: DailyCount[];
};

export type CategoriesResponse = { categories?: CategoryData[] };
export type HistoryResponse = { items?: HistoryItem[] };
export type QueueResponse = { items?: QueueItem[]; posts_today?: number; total_in_flight?: number };
export type ScheduleResponse = {
  slots?: ScheduleSlot[];
  posts_per_day?: number;
  interval_minutes?: number;
};

export type ReuseResponse = {
  total_active: number;
  never_posted: number;
  posted_once: number;
  posted_multiple: number;
  reuse_rate: number;
  never_posted_by_category: { category: string; dead_count: number }[];
};

export type DeadContentResponse = {
  total_active?: number;
  total_dead?: number;
  dead_percentage?: number;
  by_category?: { category: string; dead_count: number; total_count?: number }[];
};

/**
 * The workspace's own settings, as the settings screen reads them.
 *
 * Every field is optional and every call site supplies a default, which is how
 * the untyped version behaved. That is worth flagging rather than tidying: a
 * default here is indistinguishable from a value the workspace actually holds,
 * so a field the router stops sending silently reverts the UI to 3 posts a day
 * and shows it as the current setting. Correct against the real routes when
 * they land, and prefer required fields where the router guarantees them.
 */
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

import type { InstagramAccount } from "./types";

export type AccountsResponse = { accounts?: InstagramAccount[] };
