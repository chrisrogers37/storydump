/**
 * A DESTINATION — the Instagram account a workspace schedules for, as the
 * target API actually returns it (`GET /workspaces/{ws}/accounts`).
 *
 * Separate from `InstagramAccount`, which is the LEGACY payload shape and now
 * has one consumer left (the unreachable setup wizard). They are not two names
 * for one thing: the target sends `handle` and `state`, the legacy shape sent
 * `instagram_username` and `is_active`, and the settings screen was reading the
 * legacy field names off a target response — rendering a bare `@` for every row
 * (#1048's class). Typing the response for what it is, is the fix.
 *
 * A NARROWING, not the whole projection: `workspaces.list_accounts` also returns
 * `posts_per_day`, `posting_hours_start/end`, `tz` and `created_at`. They are
 * omitted because nothing renders them yet, not because the API stopped sending
 * them — add the field here when a screen needs it.
 */
export interface Destination {
  id: string;
  /** The identity key. `manual:<handle>` until OAuth supplies a real Meta id. */
  provider_account_ref: string;
  /** What a person typed. Null on a row created before the form existed. */
  handle: string | null;
  display_name: string | null;
  state: string;
  next_slot_at: string | null;
  last_posted_at: string | null;
  /** #1220 step 2. `none` = never connected; `expired`/`revoked` = reconnect needed. */
  credential_status: "none" | "active" | "expired" | "revoked";
  credential_connected_at: string | null;
}

/** LEGACY account payload. One consumer left; dies with it. See `Destination`. */
export interface InstagramAccount {
  id: string;
  display_name: string;
  instagram_username: string;
  is_active: boolean;
}

/** Backend instance summary returned by GET /api/instances. */
export interface Instance {
  chat_settings_id: string;
  telegram_chat_id: number;
  display_name: string;
  media_count: number;
  posts_per_day: number;
  is_paused: boolean;
  last_post_at: string | null;
  instance_role: string;
}

/** A Telegram chat this workspace's cards go to (`GET /workspaces/{ws}/bindings`). */
export interface ChannelBinding {
  id: string;
  channel: "telegram_group" | "telegram_dm" | string;
  /** The Telegram chat id, as text. */
  external_ref: string;
  state: "active" | "revoked" | string;
  created_at: string;
}

export interface BindingsResponse {
  bindings: ChannelBinding[];
}

/** A row of `GET /workspaces/{ws}/members`. `added_by_user_id` null on a member
 * who joined from a bound Telegram group (`07` §14) rather than by invitation. */
export interface WorkspaceMember {
  user_id: string;
  role: "owner" | "admin" | "member" | string;
  added_by_user_id: string | null;
  created_at: string;
  primary_email: string | null;
  user_state: string;
}

export interface MembersResponse {
  members: WorkspaceMember[];
}
