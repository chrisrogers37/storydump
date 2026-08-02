# Fixed constraints (product-owner rulings)

These are rulings, not proposals. An implementer or reviewer who believes one is wrong escalates to the product owner; nobody revises them inside the program.

## FC-0 — Envelope: thousands of tenants (prior ruling)

Design and size for **thousands of provisioned workspaces** (working figure: 5,000 provisioned, ~25% concurrently active), not the ~200 the cold design originally used and not #721's single-process non-goal. This alone mandates multi-service topology and horizontal scale-out (see `03` C2).

## FC-1 — Tenant identity: users own workspaces; workspaces own Instagram accounts

End state, verbatim in substance from the product owner (2026-08-02):

- A **user signs up** and owns **multiple workspaces**.
- Each **workspace holds multiple Instagram accounts** ("one workspace with 4 Instagram accounts, another with 3 different ones, organised cleanly").
- A **Telegram account can manage one-to-many** of these workspaces.

Consequences (normative):

1. **The tenant root is `workspaces.id`.** Every tenant-scoped table keys on it (NOT NULL). The neutral `tenant_id` name is used at all service boundaries and resolves to `workspaces.id` — never to a chat id.
2. **A Telegram group chat is a *binding* of a workspace, not the workspace.** Today's `chat_settings` row is the physical tenant; in the target model its tenant-config half migrates to `workspaces` and its chat-specific half becomes a `channel_bindings` row (see `02`). A workspace with zero Telegram chats is legal (web-only tenant).
3. **Human identity is platform-neutral.** `users` carries no Telegram column; Telegram user ids live in `user_identities(provider='telegram')`. Sign-up without Telegram must be possible.
4. Current-state measurement (verified on `origin/main`, 2026-08-02): **no workspace/tenant/org/team concept exists** — 14 model tables, flat `users → instagram_accounts`, with `chat_settings`, `user_chat_memberships`, `user_interactions` as first-class core tables. The re-key from chat-rooted to workspace-rooted is therefore a full migration track (`04` Phase W), run on the six-stage machine.

## FC-2 — Telegram is one of several interaction layers, not the substrate

Ruling: Telegram must become **one pluggable interaction layer among several**. The product owner is explicit that the system has grown too Telegram-reliant and wants this pushed deeply.

Consequences (normative):

1. **Channel-neutral core.** Domain state and domain services must not reference Telegram types, ids, or libraries. The domain vocabulary is `approval request`, `notification`, `command` — not `card`, `chat`, `callback`.
2. **Adapters at the edges.** Inbound: each channel adapter (Telegram webhook first; web/Mini-App API is already a second surface; more later) resolves external identity → (`user_id`, `workspace_id`, `channel_binding_id`) and normalizes to channel-neutral commands before anything else runs. Outbound: domain emits channel-neutral interaction requests; per-channel senders render and deliver them (`02` §outbox).
3. **Measured baseline and ratchet.** On `origin/main` (2026-08-02): **75 of 147** `src/` + `cli/` Python modules reference Telegram. A CI ratchet (installed at `04` F.6, burned down through Phase X) enforces an allowlist of modules permitted to reference Telegram: deliberate additions only (new adapter/sender modules), any reference outside it fails CI, and the core-services segment must reach empty.
4. **Structural tables migrate out of core.** `chat_settings`, `user_chat_memberships`, `user_interactions` — their channel-specific content moves to adapter-owned tables (`channel_bindings` and adapter state); their tenant/membership content moves to `workspaces` / `workspace_members`.

## FC-3 — Cloudinary is app-level; media transit is scoped, signed, and reaped

Ruling: **no per-user or per-workspace Cloudinary onboarding, ever.** One app-level Cloudinary environment. Media is transit-only (the existing design already treats it as transient: destroy-after-post, `cloud_public_id` retained for deletion, media-lifecycle service), so the blast radius of the shared environment is minutes of in-flight media, not archives.

The following are **requirements, not intentions** (each has an increment and a gate in `04`):

| # | Requirement |
|---|---|
| FC-3.1 | Per-workspace folder prefixes — every transit asset uploads under `ws/{workspace_id}/…` |
| FC-3.2 | Short-TTL **signed** delivery URLs; no unsigned delivery |
| FC-3.3 | Never public-by-default — assets upload as `type=authenticated` (or provider equivalent) |
| FC-3.4 | Upload presets scoped per workspace; no global permissive preset |
| FC-3.5 | Reap on successful post — the publish pipeline's terminal step destroys the transit asset |
| FC-3.6 | Hard TTL sweep for failures — a recurring job destroys any transit asset older than the TTL regardless of pipeline state |

Shared-environment capacity: the app-level environment plus a measured usage ceiling and a documented shard/upgrade path (from #730 §2's shared-scope finding) — capacity is an ops concern, never a user-onboarding concern.

## FC-4 — Instagram API with Instagram Login; never a Facebook Page

Ruling: design for the **Instagram API with Instagram Login** (July 2024): OAuth direct through Instagram, Instagram User access tokens, **no Facebook Page — never make a user auth a Facebook Page again**. What survives from the platform reference: the account must still be **Professional (Business or Creator)** — the funnel is exactly one conversion (personal → Professional), nothing else.

**Pre-lock verification (performed 2026-08-02 against `origin/main`, recorded here because the ruling required it):** the Facebook-Login-for-Business path is the only one offering hashtagged-media search and metadata/metrics about *other* accounts. storydump uses **neither**: zero occurrences of `ig_hashtag_search` / hashtag-search endpoints / `business_discovery`; all "hashtag" hits are caption-text composition; all "insights" hits are internal dashboard queries against our own database. Instagram Login is therefore locked with no feature loss.

What *does* touch `graph.facebook.com` today is the auth plumbing itself — `oauth_service` `/me/accounts` Page listing, `cli/commands/instagram.py` FB-flow onboarding, and FB-vintage token refresh — i.e. exactly the code Instagram Login replaces. Consequences (normative):

1. New connections use Instagram Login OAuth end-to-end (scopes: `instagram_business_basic`, `instagram_business_content_publish`; refresh via `graph.instagram.com`).
2. **Existing connected accounts hold FB-vintage tokens.** They keep working through the existing dual-path refresh, marked legacy; each account migrates to an Instagram-Login credential on its next reconnect (assisted by a re-auth campaign). Sunset gate: zero active FB-vintage credentials → delete the legacy path. No silent cutover, no forced same-day re-auth.
3. App Review for the Instagram-Login scopes starts at Phase 0 (2–4 weeks per permission, screencast each; schedule risk, not feasibility risk).
4. If any future feature appears to need hashtag search or other-account metrics, that is a product-owner escalation (it would force the Facebook-Login path) — never a silent adoption.
