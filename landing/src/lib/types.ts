/**
 * A DESTINATION — the Instagram account a workspace schedules for, as the
 * target API actually returns it (`GET /workspaces/{ws}/accounts`).
 *
 * Typed for what the target serves: `handle` and `state`, not
 * `instagram_username` and `is_active` — reading field names the response
 * does not carry renders a bare `@` for every row (#1048).
 *
 * A NARROWING, not the whole projection: `workspaces.list_accounts` also returns
 * `posts_per_day`, `posting_hours_start/end` and `created_at`. They are omitted
 * because nothing renders them yet, not because the API stopped sending them —
 * add the field here when a screen needs it.
 */
export interface Destination {
  id: string;
  /** The identity key. `manual:<handle>` until OAuth supplies a real Meta id. */
  provider_account_ref: string;
  /** What a person typed, or null when the row has none. */
  handle: string | null;
  display_name: string | null;
  state: string;
  next_slot_at: string | null;
  last_posted_at: string | null;
  /** #1220. `none` = never connected; `expired`/`revoked` = reconnect needed. */
  credential_status: "none" | "active" | "expired" | "revoked";
  credential_connected_at: string | null;
  /**
   * The account's own IANA zone. Null means it reads in the workspace's, as
   * `schedule_item` does (`COALESCE(a.tz, w.tz)`); the Schedule dialog names it.
   */
  tz: string | null;
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
 * who joined from a bound Telegram group rather than by invitation. */
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
