import { describe, it, expect } from "vitest";
import {
  deriveSettings,
  type SourceRow,
  type StatsResponse,
  type WorkspaceConfig,
} from "./dashboard-payloads";

/**
 * The Settings screen's honesty rests on one property: A FIELD WITH NO SOURCE
 * IS NEVER RENDERED AS A VALUE (#1063).
 *
 * The old page coalesced everything (`setup.X ?? default`), and that is the
 * half the hard bail cannot catch: the bail fires when a fetch FAILS, while a
 * defaulted field arrives inside a response that SUCCEEDED. `media_sync_enabled
 * ?? false` rendered the flat sentence "Auto-sync disabled" — a claim about the
 * workspace manufactured from a column that does not exist, which is exactly
 * the harm `settings/page.tsx` warns about in its own comment.
 *
 * So these assertions are about ABSENCE being preserved, not about arithmetic.
 */

const CONFIG: WorkspaceConfig = {
  id: "w1",
  name: "Stub",
  state: "active",
  tz: "America/New_York",
  posts_per_day: 6,
  posting_hours_start: 9,
  posting_hours_end: 21,
  approval_mode: "auto",
  auto_reapprove_returning: true,
  approval_ttl_minutes: 60,
  dry_run_mode: false,
  is_paused: false,
  paused_at: null,
  repost_ttl_days: 30,
  skip_ttl_days: 7,
  caption_style: "short",
  enable_ai_captions: true,
  api_publishing_enabled: true,
  offboarding_at: null,
  restorable_until: null,
  created_at: "2026-08-01T00:00:00Z",
  updated_at: null,
};

const STATS: StatsResponse = {
  intents_by_state: {},
  media_by_state: { available: 12, archived: 3 },
  media_never_posted: 5,
  media_by_category: {},
  posted_by_category: {},
  posts_by_day: [],
  accounts: 1,
  sources: 1,
};

const DRIVE: SourceRow = {
  id: "s1",
  provider: "gdrive",
  state: "active",
  next_sync_at: null,
  last_sync_success_at: null,
  alerted_at: null,
  created_at: "2026-08-01T00:00:00Z",
  folder_ref: "folder-abc",
  folder_name: "Stories",
};

const GRANT = { status: "active", connected_at: "2026-08-01T00:05:00Z" };

describe("deriveSettings keeps the unsourced fields unsourced", () => {
  it("returns null — not false, not 0 — for every field the API cannot answer", () => {
    const v = deriveSettings(CONFIG, [DRIVE], STATS);

    for (const key of [
      "show_verbose_notifications",
      "send_lifecycle_notifications",
      "media_sync_enabled",
      "gdrive_email",
      "media_source_root",
    ] as const) {
      expect(v[key], `${key} must be null`).toBeNull();
      // The distinction that matters: a caller doing `?? false` gets a
      // rendered "off", so assert the value is not already a boolean or a
      // number that a consumer would print as a measurement.
      expect(typeof v[key], `${key} must not be a value`).not.toBe("boolean");
      expect(typeof v[key], `${key} must not be a value`).not.toBe("number");
      expect(typeof v[key], `${key} must not be a value`).not.toBe("string");
    }
  });

  it("renames api_publishing_enabled rather than dropping it", () => {
    // The workspace row and the Settings screen disagree on this field's name.
    // A repoint that missed the rename would render the toggle from a default
    // while every other field was real - the hardest kind to notice.
    expect(deriveSettings(CONFIG, [], STATS).enable_instagram_api).toBe(true);
    expect(
      deriveSettings({ ...CONFIG, api_publishing_enabled: false }, [], STATS)
        .enable_instagram_api,
    ).toBe(false);
  });

  it("passes a null from the workspace row straight through", () => {
    // Sourced-but-unset is still not a default. `posts_per_day: null` means
    // the workspace has no schedule set, which the screen must show as absent
    // rather than as some number.
    const v = deriveSettings({ ...CONFIG, posts_per_day: null, caption_style: null }, [], STATS);
    expect(v.posts_per_day).toBeNull();
    expect(v.caption_style).toBeNull();
  });

  it("derives the Drive connection from the WORKSPACE's grant, not from the source row", () => {
    /*
     * #1081 moved this off `drive !== null` (a source row existing). 069 moved
     * the credential itself to the workspace, so the answer comes from
     * `GET /workspaces/{ws}/drive` and a source row says nothing about it.
     */
    const connected = deriveSettings(CONFIG, [DRIVE], STATS, GRANT);
    expect(connected.gdrive_connected).toBe(true);
    expect(connected.media_source_type).toBe("gdrive");
    expect(connected.media_source_state).toBe("active");
  });

  it("a folder without a grant is NOT connected — and is still reported as a source", () => {
    const ungranted = deriveSettings(CONFIG, [DRIVE], STATS, { status: "none", connected_at: null });
    expect(ungranted.gdrive_connected).toBe(false);
    expect(ungranted.media_source_state).toBe("active");
  });

  it("an erroring folder under a live grant is still connected", () => {
    const erroring = deriveSettings(CONFIG, [{ ...DRIVE, state: "error" }], STATS, GRANT);
    expect(erroring.gdrive_connected).toBe(true);
    expect(erroring.media_source_state).toBe("error");
  });

  it("expired and revoked are not connected — they are reconnect-needed", () => {
    for (const status of ["expired", "revoked"] as const) {
      const v = deriveSettings(CONFIG, [DRIVE], STATS, { status, connected_at: null });
      expect(v.gdrive_connected, status).toBe(false);
    }
  });

  it("an unknown grant — the status could not be loaded — is not connected", () => {
    const none = deriveSettings(CONFIG, [], STATS, null);
    expect(none.gdrive_connected).toBe(false);
    expect(none.media_source_state).toBeNull();
  });

  it("counts media across every state, not just the available ones", () => {
    expect(deriveSettings(CONFIG, [], STATS).media_count).toBe(15);
  });
});
