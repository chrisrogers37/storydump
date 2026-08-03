# Product lifecycles and multi-account semantics

The product-behavior contracts the schema alone cannot state: lifecycles, movement, multi-account scheduling, and failure behavior a customer can see. Added in the second pass (review A §5.1–9); FC-1's multi-account end state is completed here. DDL objects cited are in `02`; increments that build each behavior are named in `04`.

## §1. Workspace lifecycle

State machine on `workspaces.state` (transitions service-enforced; every transition audited):

| From | To | Actor | Semantics |
|---|---|---|---|
| — | active | signup flow (X.3) or F.2 backfill | creation = INSERT (T3); the creator's `role='owner'` member row inserts in the same transaction (ownership's single home — `02` §1) |
| active | suspended | operator, or owner (pause-everything) | clock skips it (dispatcher predicate `state='active'`); live intents are cancelled by the worker on next touch; inbound commands answered with a "suspended" notice; data untouched |
| suspended | active | operator / owner | clock resumes; nothing to rebuild |
| active/suspended | offboarding | owner (explicit, confirmed) or operator | the ordered workflow below starts; irreversible after the grace window |
| offboarding | active | owner, **within the grace window only** | restoration = flag flip; nothing was deleted yet |

**Offboarding workflow** (one `offboard_workspace` job orchestrates the legs — `02` §5 registry; **`04` X.3 builds the executor itself**, the machinery exists from Phase L). **Leg order is load-bearing (pass 3 — R3 review §3.4: the pass-2 order revoked credentials before draining publishing/ambiguous work, destroying exactly the credentials reconciliation still needed):**

1. **Drain and terminalize first, with credentials alive:** every live intent → `cancelled` (legal from every working state except publishing/ambiguous); publishing and ambiguous intents drain to terminal — the workflow waits for the publish serialization to clear and the reconciler to resolve, bounded by the `05` drain timeout. A drain that times out parks the leg and alerts the operator rather than revoking under live work.
2. `revoke_workspace_credentials` job: best-effort provider revocation per credential (IG token invalidation, Drive token revoke), retry budget per `05`; a revocation that still fails is recorded in audit (`revoke_failed`) and abandoned — the credential row dies with the workspace and the provider token expires on its own. Rows go `state='revoked'` as they process.
3. Transit reap: destroy any `transit_asset_ref` still alive (FC-3.5 path); the FC-3.6 TTL sweep is the backstop.
4. **Grace window** (`05`, initial 30 days): the workspace sits in `offboarding` — invisible to the clock, restorable by the owner.
5. Final deletion: through `fn_offboard_finalize` (`02` §7 door — guarded inside: grace elapsed, zero live intents), which deletes the `workspaces` row; the §0-policy cascade removes every tenant row. `audit_events` survives by design (no FK) under its retention clock.

**Data disposition at final deletion:** everything workspace-keyed dies with the cascade (the `02` §0 class-1 set — no separate inventory to drift). Survives: `audit_events` (retention-bounded), Cloudinary transit assets (minutes — TTL-bounded regardless), Drive content (theirs, untouched), Instagram posts (published content is the customer's; we never delete posts).

**Rename** = UPDATE `workspaces.name`, audited. No slug/identity coupling exists (ids are UUIDs everywhere).

## §2. Membership lifecycle

- **Join, Telegram path (kept from current behavior):** the group-membership sync on an active binding upserts `workspace_members(role='member')` for chat members with known identities; leaves deactivate nothing automatically (a chat kick is not a workspace removal — an admin removes membership explicitly).
- **Join, web path:** admin+ creates a `workspace_invitations` row (email, role ≤ admin); the invitee signs in via email OTP (07 §1), and accepting consumes the invitation (one-shot: `state='accepted'`, member row inserted, same transaction). Stale invitations expire via the `reap_expired` sweep (`02` §5 remit — no bespoke reaper).
- **Role change:** owner may set any role except owner (ownership moves only by transfer); admins may promote member↔admin but never touch the owner. Enforced in the one authorization gate; the DB backstop is `uq_members_one_owner`.
- **Ownership transfer:** one transaction — demote the old owner row to `admin`, promote the new owner row to `owner`. `uq_members_one_owner` (at most one) and the deferred owner-exists trigger (at least one, `02` §1) make any other shape of this operation fail at commit; the transfer command is the only writer that composes it. New owner must already be a member.
- **Removal:** admin+ removes members; nobody removes the owner (transfer first — last-owner protection is structural, not a check).
- **Disabled user (`users.state='disabled'`):** memberships and ownership persist; the ingress gate denies every request. If the disabled user is an owner and the workspace must continue, the operator transfers ownership (break-glass runbook, 07 §5).
- Every membership mutation writes `audit_events` (`entity_kind='member'`, actor + channel).

## §3. Multi-account scheduling (the FC-1 end state, made executable)

The unit of scheduling is the **account**, not the workspace. The workspace is the container: shared media pool, shared members, default settings.

- **Schedule ownership:** each `ig_accounts` row has its own cadence (`posts_per_day`), window (`posting_hours_start/end`), tz, and slot cursor (`next_slot_at`) — NULL columns inherit the workspace defaults (`02` §2). The clock's due-scan is over **accounts** (`ix_ig_accounts_due`); slot planning inserts intents per account (key 1 includes `ig_account_id`).
- **Cadence caps are per account** (R2 was already per (workspace, account, local day)): `daily_post_counts` keys on the account; `local_date` is computed in the **account's effective tz** at debit time (mechanics: `02` §4). **This bullet is the normative home for the acceptance rules:** the debit day is recorded (`cap_consumed_on`) and refunds target the recorded day, so DST transitions and tz changes can never touch the wrong bucket; a same-day tz change can shift the day boundary for *later* debits by at most one slot — accepted; a mid-day cadence (`posts_per_day`) change applies from the **next** account-local day, because `cap_at_write` freezes the day's cap at its first debit — accepted (the alternative, retroactive re-capping, would make an in-flight day's arithmetic unstable).
- **Media selection:** one workspace pool. Selection for account A's slot draws from `media_items` (state `available`, category mix per the workspace's SCD table) minus workspace-wide locks (skip/reject/seasonal/hold/unsupported) minus **A's own recent locks**. `recent` locks are account-scoped (`post_locks.ig_account_id`): account A posting item X yesterday does not block account B posting X today — by design; a workspace that wants cross-account spacing sets its repost TTL accordingly and the workspace-wide kinds remain available to humans.
- **One media item → several accounts:** legal and first-class (key 2 includes the account). Each account's posting of the item is its own intent, its own approval, its own outcome.
- **Manual-mode workspaces (the live phase-1 flow, carried forward — pass-2 addition):** a workspace with the Instagram-API routing flag off runs the same ledger up to `awaiting_approval`; its card offers **Posted / Skip / Reject** instead of approval-for-publishing. The Posted tap (`mark_posted` command) terminalizes the intent directly — `published_via='manual'`, cap debited, recent lock written, no publish pipeline, no Meta traffic (`02` §4 matrix edge). Hybrid workspaces (API on) keep the manual buttons as the fallback path, exactly as today.
- **Approvals identify the target account:** the approval prompt payload carries `ig_account_id` + handle (`channel_outbox.payload`); a workspace with four accounts sees four distinguishable cards (or one web list with an account column). Auto-approval policy (`approval_mode`, `auto_reapprove_returning`) is workspace-level in v1 — per-account approval policy is a plain column migration if product wants it later (the inheritance pattern already exists).
- **Account disabled/reauth_required:** the dispatcher skips non-`active` accounts (its due-scan predicate); live intents for the account are cancelled by the worker on next touch (matrix edge "workspace/account disabled"); `awaiting_approval` cards for it supersede with a notice. Re-enabling resumes the slot cursor from `next_slot_at` recomputation.
- **Interaction cost note (review A §5.4 asked who is notified):** prompts/notifications go to the **workspace's** bindings — the workspace is the collaboration unit; there is no per-account notification routing in v1 (recorded as a deliberate simplification; per-account routing would be a `channel_outbox` audience field later, not a schema change now).

## §4. Account movement between workspaces (X.3; PA-1-compatible)

Movement is **clone-and-retire** — never an in-place re-key (composite FKs make history immovable by construction, deliberately):

1. Preconditions: actor holds admin+ in **both** workspaces; source account has zero live intents (the command offers cancel-all; publishing/ambiguous must drain first).
2. One transaction: insert the target-workspace `ig_accounts` row (same `provider_account_ref`, fresh id, schedule columns copied); **move the credential — insert the target-workspace `oauth_credentials` row with the copied ciphertext AND set the source credential row `state='revoked'` in the same transaction** (pass 3 — R3 review §6.9: the pass-2 copy left two active rows holding one grant, free to diverge under refresh; a move leaves exactly one active row, so refresh has one home); mark the source account `state='moved'` (terminal tombstone, excluded from `uq_ig_account_live` so the account can later return); revoke nothing **at the provider** — the token moved, it didn't die; `'revoked'` on the source row is our bookkeeping, not a provider call. Audit rows in both workspaces (`entity_kind='ig_account'`, detail: from/to workspace ids, actor).
3. What does NOT move: history (terminal intents, audit, counts stay in the source workspace — history belongs to the tenant where it happened); locks (`recent` locks are account-scoped but reference source-workspace media rows — the target workspace has its own media pool; fresh start); the source's `daily_post_counts` (the target starts fresh — Meta's own rolling cap remains the true arbiter across the move, via error 9).
4. Product cap note: immediately after a move, the target workspace's fresh product-cap day could admit publishes beyond what the source already consumed today; Meta's cap catches the excess (error-9 deferral). Accepted — our cap is cadence policy, not the provider budget (`02` §8).

Under PA-1(a) (independent connections — the default this plan implements) movement is a convenience composition of connect+disconnect that preserves the credential; under PA-1(b) (global single-ownership) the same protocol runs with the extra precondition that no other live row exists. The protocol is identical either way — the fork only changes the connect-time uniqueness rule.

## §5. Customer-visible failure behavior (review A §5.20)

| Situation | What the customer sees | Mechanism |
|---|---|---|
| `review_required` intent | after `05`'s **customer-notification window (24 h — distinct from the 15-min operator page)**: one workspace notification ("a post needs attention", deep link to the web queue); daily digest while any remain | the reconciler sweep writes the outbox `notification` when the window passes (it already walks `ix_intents_parked` — no extra producer); operator surface below |
| Cap-deferred publish (product cap or error 9) | nothing per occurrence (by design — deferral is normal operation); the web queue shows the intent `approved` with its next attempt time | queue view reads intents + jobs |
| Slot missed (workspace paused, account disabled, no media) | the slot lapses silently; `expired` intents are visible in history; a "no media available" notification fires at most once per `05` window when selection returns empty | slot planner + notification dedup |
| No reachable surface (all deliveries failed, no web access) | intent `failed` (matrix edge); one workspace-level alert on the binding that still works, if any; otherwise it is visible on next web login | prompt_pending→failed edge + alert |
| Re-auth needed (FC-4 campaign or revoked credential) | account card in web + one prompt per `05` campaign cadence on the bindings; posting for that account pauses (dispatcher skips `reauth_required`) | G.1 campaign / credential state machine |
| Operator resolution surface | web (Mini-App) admin view: `review_required` list with the full `last_error.evidence` trail, actions resolve-posted / resolve-failed / retry (new generation) / cancel — each an audited command | X.2 surface + `02` §4 matrix edges |

## §6. Explicit non-goals (named so their absence is a decision, not an omission)

- **Billing / plans / entitlements** (review A §5.9): out of scope for this program, deliberately (carried from #721's deferral — no ruling exists to build it). The v1 limiter is the `05` per-workspace admission + concurrency bounds, which are abuse guards, not entitlements. The extension point when product gets here: a `workspace_limits` row-set consulted by the same admission path — no schema reserved now (YAGNI; the admission seam is the anchor).
- **User data export / GDPR-style deletion** (review A §5.7): v1 ships account-disable + ownership-transfer + workspace offboarding (which deletes tenant data). Personal-data export and user-row erasure are **deferred with product-owner visibility** — flagged as a pre-GA requirement in the program's closing report; identity unlink (delete `user_identities` row after transfer) is the only v1 erasure door.
- **Data locality / regional compliance** (review A §5.22): the deployment is US-hosted (Railway + Neon US); no regional storage or deletion-SLA obligations are adopted in v1. Explicitly unknown whether future customers impose them — product-owner flag, same closing report.
- **Per-account notification routing** and **per-account approval policy**: deliberate v1 simplifications, §3 above, each one column/field away.

## §7. Support and admin access, v1 posture (review A §5.21)

One operator (the product owner). No impersonation feature exists — the operator acts through the admin API surface with their own identity (`actor_kind='operator'` in audit) or, break-glass, through the documented psql runbook (07 §5: session GUCs set manually so the 02 §4 audit trigger still names the actor; the runbook is the only sanctioned raw-SQL door). Every operator action on tenant data is audit-visible to that tenant's admins in the web audit view. Multi-operator tooling, support roles, and scoped impersonation are explicitly out of v1 scope — the audit substrate they would need is what this plan builds.
