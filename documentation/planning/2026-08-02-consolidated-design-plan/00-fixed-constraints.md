# Fixed constraints (product-owner rulings)

These are rulings, not proposals. An implementer or reviewer who believes one is wrong escalates to the product owner; nobody revises them inside the program.

## FC-0 — Envelope: thousands of tenants (prior ruling)

Design and size for **thousands of provisioned workspaces** (working figure: 5,000 provisioned, ~25% concurrently active), not the ~200 the cold design originally used and not #721's single-process non-goal. The envelope mandates horizontal scale-out; the three-role split additionally rests on fault-isolation and deploy-independence grounds stated in `03` C2 (as amended in pass 2 — tenant cardinality alone does not mandate role separation).

## FC-1 — Tenant identity: users own workspaces; workspaces own Instagram accounts

End state, verbatim in substance from the product owner (2026-08-02):

- A **user signs up** and owns **multiple workspaces**.
- Each **workspace holds multiple Instagram accounts** ("one workspace with 4 Instagram accounts, another with 3 different ones, organised cleanly").
- A **Telegram account can manage one-to-many** of these workspaces.

Consequences (normative):

1. **The tenant root is `workspaces.id`.** Every tenant-scoped table keys on it (NOT NULL). The neutral `tenant_id` name is used at all service boundaries and resolves to `workspaces.id` — never to a chat id.
2. **A Telegram group chat is a *binding* of a workspace, not the workspace.** Today's `chat_settings` row is the physical tenant; in the target model its tenant-config half migrates to `workspaces` and its chat-specific half becomes a `channel_bindings` row (see `02`). A workspace with zero Telegram chats is legal (web-only tenant).
3. **Human identity is platform-neutral.** `users` carries no Telegram column; Telegram user ids live in `user_identities(provider='telegram')`. Sign-up without Telegram must be possible.
4. Current-state measurement (verified on `origin/main` 2026-08-02; re-verified at the pass-4 anchor, `main` @ `2e13f97`): **no workspace/tenant/org/team concept exists** — 14 model tables, with `chat_settings`, `user_chat_memberships`, `user_interactions` as first-class core tables. The account hierarchy is flat and **chat-mediated**: no user-level ownership edge exists — accounts are selected per chat (`chat_settings.active_instagram_account_id`) and credentials are owned per chat (`api_tokens.chat_settings_id`). The re-key from chat-rooted to workspace-rooted is therefore a full migration track (`04` Phase W), run on the six-stage machine.

## FC-2 — Telegram is one of several interaction layers, not the substrate

Ruling: Telegram must become **one pluggable interaction layer among several**. The product owner is explicit that the system has grown too Telegram-reliant and wants this pushed deeply.

Consequences (normative):

1. **Channel-neutral core.** Domain state and domain services must not reference Telegram types, ids, or libraries. The domain vocabulary is `approval request`, `notification`, `command` — not `card`, `chat`, `callback`.
2. **Adapters at the edges.** Inbound: each channel adapter (Telegram webhook first; web/Mini-App API is already a second surface; more later) resolves external identity → (`user_id`, `workspace_id`, `channel_binding_id`) and normalizes to channel-neutral commands before anything else runs. Outbound: domain emits channel-neutral interaction requests; per-channel senders render and deliver them (`02` §outbox).
3. **Measured baseline and ratchet.** On `origin/main` (2026-08-02): **75 of 147** `src/` + `cli/` Python modules reference Telegram (76 at the pass-4 anchor `2e13f97` — one merge since; F.6 re-measures at install). A CI ratchet (installed at `04` F.6, burned down through Phase X) enforces an allowlist of modules permitted to reference Telegram: deliberate additions only (new adapter/sender modules), any reference outside it fails CI, and the core-services segment must reach empty.
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

FC-3.4's **delivery mechanism** is decided in `03` D28 (server-signed per-request upload parameters; ratification status and the literal-presets fallback live there). D28 interprets the ruling's mechanism; it does not revise the ruling.

## FC-4 — Instagram API with Instagram Login; never a Facebook Page

Ruling: design for the **Instagram API with Instagram Login** (July 2024): OAuth direct through Instagram, Instagram User access tokens, **no Facebook Page — never make a user auth a Facebook Page again**. What survives from the platform reference: the account must still be **Professional (Business or Creator)** — the funnel is exactly one conversion (personal → Professional), nothing else.

**Pre-lock verification (performed 2026-08-02 against `origin/main`, recorded here because the ruling required it):** the Facebook-Login-for-Business path is the only one offering hashtagged-media search and metadata/metrics about *other* accounts. storydump uses **neither**: zero occurrences of `ig_hashtag_search` / hashtag-search endpoints / `business_discovery`; all "hashtag" hits are caption-text composition; all "insights" hits are internal dashboard queries against our own database. Instagram Login is therefore locked with no feature loss.

What *does* touch `graph.facebook.com` today is the auth plumbing itself — `oauth_service` `/me/accounts` Page listing (and the same flow's dialog/exchange/page fetches), `cli/commands/instagram.py` FB-flow onboarding, FB-vintage token refresh, token revocation (`DELETE /me/permissions`, `token_refresh.py`), and the Mini-App manual-token validation fetch (`api/routes/onboarding/settings.py`) — i.e. exactly the credential plumbing Instagram Login replaces; no feature-level Facebook-graph use exists (pass-4 anchor: the last two sites were missing from this enumeration). Consequences (normative):

1. New connections use Instagram Login OAuth end-to-end (scopes: `instagram_business_basic`, `instagram_business_content_publish`; refresh via `graph.instagram.com`).
2. **Existing connected accounts hold FB-vintage tokens.** They keep working through the existing dual-path refresh, marked legacy; each account migrates to an Instagram-Login credential on its next reconnect (assisted by a re-auth campaign). Sunset gate: zero active FB-vintage credentials → delete the legacy path. No silent cutover, no forced same-day re-auth.
3. App Review for the Instagram-Login scopes starts at Phase 0 (2–4 weeks per permission, screencast each; schedule risk, not feasibility risk).
4. If any future feature appears to need hashtag search or other-account metrics, that is a product-owner escalation (it would force the Facebook-Login path) — never a silent adoption.

## FC-5 — Web sign-in: Google sign-in replaces email OTP; Apple descoped (ruling 2026-08-03)

Ruling: web sign-in is **Google sign-in, not email OTP** ("we already have a Google App"); Apple was considered and **descoped** with the review panel's case in hand. Applied via the analysis on PR #731 (comments 5168818430 / 5170243666). Consequences (normative):

1. **Sign-in ships Google-only at X.3.** The OTP machinery — challenge table, issue/verify flows, the OTP rate scopes, the `email_otp` identity provider — is out of the plan entirely. Sessions survive unchanged: sign-in still ends by issuing exactly the `07` §1 session.
2. **Apple is descoped, not deferred** — no reserved enum values, no pre-built increment. Its re-entry cost is recorded in `03` D34: one provider CHECK value + one flow increment + one row in the D33 acceptance-constraint table.
3. **Identity keys on the provider's immutable subject** (`external_id` = OIDC `sub`), never on email (`03` D32); account linking is explicit-only (`03` D35).
4. **Pre-lock verification (recorded like FC-4's):** a web-application Google OAuth client already exists — `GOOGLE_CLIENT_ID`/`GOOGLE_CLIENT_SECRET` in `src/config/settings.py` driving the Drive redirect flow — so sign-in reuse is a second client ID under the same GCP project and consent screen, adding only the non-sensitive `openid email profile` scopes (no Google verification burden; the sensitive-scope burden is the Drive side, carried independently of this ruling). Owner item (`03` pass-4 items): the consent screen must be in **In production** publishing status — a prerequisite the multi-tenant Drive design already carries (Testing mode expires refresh tokens after 7 days), surfaced by this ruling rather than created by it.

## FC-6 — Invitations: the app delivers, by email and Telegram (ruling 2026-08-03)

Ruling: workspace invitations are **delivered by the app** — email, plus a Telegram option — option B of the delivery fork, ruled with the review panel's case in hand; the panel's *data model* (token as the accept credential) is adopted, its *delivery* recommendation (out-of-band links) is overruled. Consequences (normative):

1. **The EmailSender port survives** with invitations as its consumer, and the email-provider ack (Resend as the swappable default) **reopens** as an owner item — surfaced, not silently re-closed (`03` pass-4 items).
2. **Telegram delivery rides existing machinery only:** one new `channel_outbox` kind on the existing sender (`02` §6). Zero new senders, zero parallel delivery paths.
3. **The accept credential is the invitation token** (`token_hash`), never the email address; email (and the Telegram-side hint) act as per-provider acceptance *constraints* — `03` D33, the day-one rule that makes Apple re-entry a flow increment rather than a model change.
4. **Invitations carry member or admin (ruled 2026-08-03: "we want both"; never owner).** An admin invitation is a *ceiling* gated by `03` D36's elevation rule — a forwarded or screenshotted admin link cannot silently produce an admin.
