---
type: audit
status: completed
owner: chris
created: 2026-09-06
tags: [investigation, drive, media-sync, telegram, production]
---

# The first real Drive sync reported success and the library stayed empty

**Symptom (owner, 2026-09-06):** Settings › Integrations showed the picked folder
"storydump-media" with *Last synced 9/6/2026, 10:34 AM*; Media Library showed
0 items. Separately, nothing had reached the bound Telegram group since it was
bound on 2026-09-05.

**Method:** read-only queries against the production database through the
linked Railway service (`railway run -- psql "$DATABASE_URL"`; the connection
string never left Railway's environment), plus a code trace. No writes.

## Findings

| # | Category | Finding | Confidence | Evidence |
|---|---|---|---|---|
| 1 | Empty library | Both picked folders ("Memes", then removed; "storydump-media") listed to completion with **zero image/video files directly inside them**. The sync listed a folder's direct children only; the legacy product walked each subfolder and used its name as the category. | High | `media_items` 0 rows; both sources' `sync_checkpoint = {"v":1}`; 4 `sync_media_source` jobs `succeeded`; `google_drive_adapter._listing_query` = `'<folder>' in parents` |
| 2 | Silent group | The empty-library notice is once per account per 24 h; the marker was stamped 2026-09-05 18:00:10 UTC by a slot that ran **before** the group was bound (19:48 UTC), so the 2026-09-06 18:00:01 UTC slot missed the window by nine seconds. | High | `ig_accounts.last_no_media_notice_at`; `channel_bindings.created_at`; no `channel_outbox` rows |
| 3 | Worker | Healthy: `plan_slot` at every UTC slot since 2026-09-02; `reconcile_ambiguous` every minute; syncs succeeded. Three `review_required` slot jobs are the "nobody to tell" outcomes from before the binding. | High | `jobs` |
| 4 | Refresh leg | Working: the grant's access token renewed at 19:42 UTC (#1250). | High | `oauth_credentials.updated_at`/`expires_at`; audit row by `system` |
| 5 | Schedule | Workspace on UTC → slots at 10 AM / 2 PM / 6 PM Eastern. | High | `workspaces.tz` |

## Decision (owner)

Build the legacy subfolder walk, with per-subfolder percentage weights for
relative posting frequency ("memes"/"merch", 70/30) — built better on the
target tier: the walk tags `media_items.category`, the weights live in
`category_post_case_mix` (D23), the planner draws by them, the web sets them.
Recorded in `03` (post-ratification rulings, 2026-09-06). Not built now:
the notice-timer fix (do not start the 24 h clock on an attempt nobody could
receive) and a folder media count in the picker — filed as follow-ups.

## What was not wrong

Not a credential, worker, or display defect; the sync, the grant, the
refresh and the clock all behaved as built. The gap was the walk, and the
weights the legacy product had and the target tier had not yet grown.
