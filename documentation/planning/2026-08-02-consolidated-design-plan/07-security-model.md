# Security model

The auth, secrets, and integrity designs review A §5.10–14 found missing. Same DDL conventions as `02` §0. Everything here is v1-executable; each mechanism names its increment in `04`.

## §1. Web sign-in: Google OIDC + sessions (X.3; session infrastructure shared with X.2's web surfaces)

Sign-in is **Google sign-in, not email OTP** (FC-5). The session half of this section is unchanged from pass 3 — sign-in still ends by issuing exactly the session below; what changed is how a user proves who they are, and that the pre-auth email surface (challenges, codes, OTP rate scopes) no longer exists.

```sql
CREATE TABLE session_tokens (
  id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id       UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  token_hash    TEXT NOT NULL,                  -- SHA256 of the opaque cookie value
  expires_at    TIMESTAMPTZ NOT NULL,           -- now() + 30 days (05 seam), sliding on use
  revoked_at    TIMESTAMPTZ NULL,
  last_seen_at  TIMESTAMPTZ NULL,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT uq_session_token UNIQUE (token_hash)
);
CREATE TRIGGER tg_touch_session_tokens BEFORE UPDATE ON session_tokens
  FOR EACH ROW EXECUTE FUNCTION trg_touch_updated_at();
```

- **The flow (server-side confidential client — X.3):** `GET /auth/google` issues an anonymous `oauth_states` row (`purpose='signin'`, §2) and redirects to Google's authorization endpoint; the callback exchanges the code server-side (the client secret never leaves the server), verifies the `id_token` against Google's published JWKS — `iss`, `aud`, `exp`, and the `nonce` binding stated in §2 — and signs the user in. JWKS is fetched from Google's discovery endpoint and cached with its HTTP cache headers; **three** OIDC hosts join the egress allowlist at X.3 (`04`): `accounts.google.com`, `oauth2.googleapis.com`, and `www.googleapis.com` — the discovery document's `jwks_uri` lives on the third (`https://www.googleapis.com/oauth2/v3/certs`, verified against the live discovery document 2026-08-04; the pass-4 two-host list would have blocked ID-token verification under the strict egress floor — R4 finding). The OIDC verification utility is the only genuinely new code class in this ruling, and it is small.
- **Identity (D32 — `sub`, never email):** success upserts `user_identities(provider='google', external_id = <OIDC sub>, verified_at = now())`, creating the `users` row on first sign-in. `external_id` is the provider's immutable subject — **never the email address**: emails are mutable and recyclable, so keying identity on email is an account-takeover primitive. The verified email claim is metadata, refreshed at each sign-in; `users.primary_email` fills from it when NULL; a claim colliding with a *different* user's `primary_email` surfaces as an error — it never merges accounts (D35).
- **Sessions:** opaque random 256-bit value in an httpOnly/SameSite=Lax/secure cookie; only the hash is stored; verification is one indexed lookup + expiry/revocation check; sliding renewal. Sign-out and admin revoke set `revoked_at`. There is no JWT for human web sessions — and none exists anywhere on `main` today (pass-4 anchor: current API auth is HMAC-signed WebApp init-data + signed URL tokens, `src/utils/webapp_auth.py`); the machine/consumer surfaces carry `workspace_id` in their signed payloads (born workspace-aware at X.2 — the pass-4 dual-shape migration window died with FC-7), and first-party service auth is §6's `service_tokens`.
- **Pre-auth rate limiting:** the sign-in endpoints ride the `preauth_ip` scope (`rate_counters`, `02` §6; the `05` pre-auth row; the client-IP source rule is stated once at the `02` §6 table) — a mechanism deliberately distinct from the per-workspace S.2 admission, which is fail-closed on tenant context and structurally cannot serve unauthenticated requests. The OTP-specific scopes died with OTP.
- **Recovery:** account recovery is Google's problem — a strictly stronger posture than pass 3's "losing the mailbox loses the account". Email *change* ceases to exist as a flow: email is a provider claim, not stored credential material. Telegram-identity users are unaffected (different provider row).
- **Linking (D35 — explicit-only, stated once here):** identities attach to a user only through an action performed inside that user's authenticated session — §2's `link` purpose covers both directions (Telegram-first → Google via OAuth redirect; Google-first → Telegram via the start-token transport). No email auto-merge exists, in any direction; a (provider, subject) already attached to another user rejects with "already linked elsewhere" (`uq_identity_per_provider`); merging two populated users is an operator action with an audit trail, explicitly out of v1.

**Email delivery — the `EmailSender` port (consumer: invitations — FC-6).** One port: `send(to, template, params) → provider_message_ref`, drained by `send_email` jobs (`02` §5 registry — interactive lane: the inviter is mid-flow awaiting send confirmation; payload carries everything, no tenant reads at send time). **Named default provider: Resend** — no email infrastructure exists in this repository today and the integration is one authenticated POST. **The volume claim, quantified (pass 5 — R4 finding):** post-FC-5/FC-6, email = invitations + bounce notices only, and launch volume is a rounding error against the free tier (`05` names the tier numbers and the volume model) — but the tier **pauses sending at quota**, and a cohort-onboarding burst crosses it exactly when the product is succeeding, silently stranding invitation delivery. The `05` email budget row + the `rate_counters` `email_global` scope (`02` §6) exist so the app defers under its own budget instead of tripping the provider's pause. **A new external service is a flagged decision, not an assumption: the owner ack is now RULED, in the deferral direction (`03`) — email is deferred, its stated intended use being referrals and other communications rather than invitation transport. The port keeps the provider swappable and SHIPS INERT until a provider is chosen.** Retry budget per `05`. The bounce/complaint webhook (ingress-hosted) now targets invitations: a bounced invitation email writes an audit event and notifies the inviter via an outbox `notification` on the workspace's binding — the pass-3 behavior (invalidating a live challenge) retired with the challenges themselves. Sender-domain setup (SPF/DKIM/return-path) travels with the provider decision; **it is no longer X.3's runbook item, and X.3's gate no longer delivers a real invitation email — that conjunct is deferred out (`04`).** The port, the `send_email` producer and this whole paragraph's machinery are BUILT: what is absent is a chosen provider, not the work. **The cost of running without one is recorded at #1130 and is not discharged by the deferral.**

## §2. OAuth flows: state tokens, sign-in/link states, and reconnect binding (L.6; signin/link widening at X.3)

One state machine serves five purposes (`bind` joined on 2026-09-05 — §13). The pass-3 table could not serve sign-in structurally (`user_id`/`workspace_id` NOT NULL, session-bound issuance) — FC-5 **widens** it rather than growing a parallel table: purposes gain `signin`/`link`, context nullability becomes purpose-conditional, and the one thing session binding used to provide (CSRF) gets a purpose-appropriate replacement for the anonymous case.

```sql
CREATE TABLE oauth_states (
  state         TEXT PRIMARY KEY,               -- 128-bit urlsafe random
  user_id       UUID NULL REFERENCES users(id) ON DELETE CASCADE,
  workspace_id  UUID NULL REFERENCES workspaces(id) ON DELETE CASCADE,
  provider      TEXT NOT NULL CONSTRAINT ck_oauth_state_provider
                CHECK (provider IN ('ig_login','gdrive','google','telegram')),
  purpose       TEXT NOT NULL CONSTRAINT ck_oauth_state_purpose
                CHECK (purpose IN ('connect','reconnect','signin','link')),
  reconnect_target UUID NULL,                   -- ig_account_id | media_source_id when purpose='reconnect'
  cookie_nonce_hash TEXT NULL,                  -- SHA256 of the browser nonce cookie (signin CSRF below)
  expires_at    TIMESTAMPTZ NOT NULL,           -- now() + state-token TTL (05 seam, all purposes)
  consumed_at   TIMESTAMPTZ NULL,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  -- purpose-conditional context (the closed-CHECK convention, 02 §0): signin is anonymous by
  -- definition; link pins the user but no workspace (identity is user-plane, not tenant-plane);
  -- connect/reconnect pin both, exactly as before:
  CONSTRAINT ck_oauth_state_context CHECK (
    CASE purpose
      WHEN 'signin' THEN user_id IS NULL     AND workspace_id IS NULL
      WHEN 'link'   THEN user_id IS NOT NULL AND workspace_id IS NULL
      ELSE               user_id IS NOT NULL AND workspace_id IS NOT NULL
    END),
  CONSTRAINT ck_oauth_state_signin_nonce CHECK (
    (purpose = 'signin') = (cookie_nonce_hash IS NOT NULL))
);
CREATE TRIGGER tg_touch_oauth_states BEFORE UPDATE ON oauth_states
  FOR EACH ROW EXECUTE FUNCTION trg_touch_updated_at();
```

RLS class: `session_tokens`, `oauth_states`, and `service_tokens` are **auth-plane tables** — role-scoped `USING (true)` policies for `svc_ingress` and the sweep door's NOLOGIN owner (`02` §7), no tenant RLS, because they are the door tenant context walks through (`02` §7 states the class; their expiry/retention classes are `05` rows, and their sweep runs only through `fn_auth_plane_sweep` — the `02` §7 door, whose NOLOGIN owner carries the enumerated auth-plane policies, driven on the reaper/retention schedule).

- **Issuance, by purpose.** `connect`/`reconnect`: only from an authenticated session whose user holds admin+ in `workspace_id` (checked at issue AND at callback — the row pins both, so a callback cannot be replayed into a different workspace). `link`: only from an authenticated session; the row pins the user, and the callback attaches the new identity to exactly that user (D35). `signin`: anonymous — that is its purpose; its guards are the next bullet plus the `preauth_ip` admission (§1).
- **Anonymous-state CSRF (`signin` — the replacement for session binding):** the issue response sets a short-TTL httpOnly cookie carrying a random nonce and stores its hash in `cookie_nonce_hash`; the callback requires the double-submit (cookie present, hash matches the row) — a cross-site victim's browser would carry no matching cookie for an attacker-supplied state. The id_token is additionally bound to the row via the OIDC `nonce` claim: the authorization request sends `nonce = SHA256(state)`, and verification requires the claim to equal the hash of the state the callback presented — a token minted for one state row cannot be replayed against another. Everything else — one-shot CAS consume, TTL, reaper — is the same machinery every purpose uses.
- Callback consume is one-shot CAS (`… WHERE state = :s AND consumed_at IS NULL AND expires_at > now() RETURNING …`); a consumed/expired/unknown state is rejected cold. For session-bound purposes CSRF safety comes from the state being unguessable, single-use, and session-bound; for `signin` it comes from the cookie-nonce + OIDC-nonce pair above.
- **The start-token door (one door, two named purposes — D33/D35):** the Telegram deep link `t.me/<bot>?start=<payload>` serves two flows, kept apart by a payload prefix that names the purpose. `inv-<token>` resolves **only** against `workspace_invitations.token_hash` (membership — `02` §1, `06` §2); `link-<state>` resolves **only** against `oauth_states` rows with `purpose='link', provider='telegram'` — the state value *is* the one-shot start token (unguessable, stored, CAS-consumed; a stateless signed token could not be one-shot). `/start` with a link token binds the tapping Telegram identity to the row's pinned user. An invite token cannot link identities and a link token cannot grant membership — enforced by disjoint lookup tables, not convention.
- **Reconnect binding:** `purpose='reconnect'` pins the exact credential owner being replaced; the callback transaction swaps `encrypted_payload` in place (same row id — no window where the account has zero credentials) and flips `ig_accounts.state` `reauth_required → active`. **Concurrent reconnects — "last issued wins", made true of the schema (pass 3; R3 review §6.6: the pass-2 "last consumed wins" claim was false — independently issued state rows never consumed one another, so both callbacks could land):** issuing a reconnect state **invalidates prior live states for the same target in the issue transaction** — `UPDATE oauth_states SET consumed_at = now() WHERE purpose = 'reconnect' AND reconnect_target = :target AND consumed_at IS NULL` — so at most one live state exists per target at any commit; the callback CAS consumes it one-shot, and a superseded state's callback finds it consumed and shows "a newer reconnect superseded this one". Connect-vs-reconnect races on the same account still collapse on `uq_credential_per_account`.

## §3. Credential encryption and key rotation (review A §5.12; L.6 + a standing runbook)

- **Mechanism: MultiFernet — which `main` already ships** (pass-4 anchor; the pass-3 "extends the existing Fernet usage" understated it): `src/utils/encryption.py` already wraps an ordered key list in `MultiFernet` under env **`ENCRYPTION_KEYS`** (comma-separated, newest first; single-key `ENCRYPTION_KEY` fallback), with a documented rotation runbook and a `storydump-cli rotate-keys` sweep. **The plan keeps the shipped env name `ENCRYPTION_KEYS`** — the pass-3 draft's `CREDENTIAL_KEYS` was an unflagged rename costing coordinated config churn on both services for zero semantic gain. Index 0 encrypts, all entries decrypt; no per-row key-id column — Fernet ciphertext self-identifies against the ring by trial, and the operational question ("which rows still need the old key") is answered by the rotation job's progress, not a column.
- **Rotation runbook:** (1) prepend the new key to `ENCRYPTION_KEYS`, deploy; (2) enqueue `reencrypt_credentials` (system job, `02` §5 registry — this job replaces the CLI sweep as loops land on the jobs machinery): batched sweep re-encrypting every row with key 0 (MultiFernet `rotate()`), progress in audit; (3) when the sweep reports zero remaining, remove the retired key from env, deploy. Rotation is therefore two config deploys around one job — no schema change, no downtime, safe to abort mid-way (old key still in ring until step 3).
- **Missing-key failure:** a payload no ring entry decrypts (key removed too early, restored backup from before rotation) fails closed — the credential flips `state='expired'`, the account flips `reauth_required`, the re-auth path recovers it. Never guess, never log ciphertext.
- **Least privilege:** `encrypted_payload` is column-SELECTable only by the roles that must decrypt (`02` §7 grant matrix); the clock schedules refreshes under its payload-free column-level SELECT grant (`02` §7-DDL — the pass-5 replacement of the earlier view). Payloads never appear in logs, traces, audit `detail`, or error payloads — the §5 hygiene rule, test-enforced.

## §4. Audit integrity and retention (review A §5.13)

- **Append-only in the database:** no role holds UPDATE on `audit_events`; DELETE only via `svc_maintenance`'s retention sweep (`02` §7). The `02` §4 audit trigger's GUC requirement means every state change carries a named actor — including break-glass psql sessions (below).
- **Retention:** `05` table — audit rows kept 400 days, then swept via `fn_retention_batch` (`02` §7). Before each sweep batch is deleted it is COPY-exported **into the in-database `archive` schema** as a batch table (`05` §DR names the location; the rationale and the properties that decided it are `03` D30); **the sweep aborts if the export fails — export-or-abort, never delete-then-hope**. No login role holds any grant on the `archive` schema: writes happen only inside `svc_maintenance`-owned door bodies and `svc_migration` contract migrations; reads are the break-glass runbook (§5). Aged archive tables are dropped *as tables* per their `05` retention rows. Queryability of archives is explicitly not a v1 feature.
- **Redaction rule:** `detail` JSONB never contains secrets, tokens, invitation-token values, or `provider_account_ref` (internal UUIDs only); enforced by the writer helper everything routes through + a test that greps captured audit output in the harness. Tamper evidence beyond grants (hash chains, signed exports) is explicitly not v1 — the stated integrity level is "no role can rewrite history without leaving a grant violation," which is what the grant matrix delivers.

## §5. Existence-oracle and log hygiene (review A §5.14)

The deliberate cross-tenant keys are inventoried in `02` §7 (three, with per-key leak analysis; the material one — `uq_publish_exclusive` — is swallowed into the defer path and never surfaces to a user). The hygiene rule that keeps the rest of the output surface oracle-free:

- Logs, metrics labels, user-visible errors, and audit `detail` reference internal UUIDs (`ig_accounts.id`, `workspaces.id`) — never `provider_account_ref`, handles being user-chosen display data are fine.
- Provider identifiers appear exactly twice in the system's output surface: inside `oauth_credentials` (encrypted) and inside provider adapter calls. A grep-shaped CI check (F.6 ratchet mechanism, second pattern list) holds `provider_account_ref` out of logging call sites.
- Admin/operator queries run through the audited surfaces (§7 of `06`); the break-glass psql runbook requires `SET app.actor_kind = 'operator'` (the audit trigger refuses anonymous writes) and is itself logged at the Neon level.

## §6. First-party API auth for CLI and operator surfaces (review A §3.31; X.2)

```sql
CREATE TABLE service_tokens (
  id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  name         TEXT NOT NULL,                   -- 'cli-<host>', 'ops-dashboard', …
  token_hash   TEXT NOT NULL,                   -- SHA256 of the opaque bearer value
  role         TEXT NOT NULL CONSTRAINT ck_service_token_role CHECK (role IN ('operator','readonly')),
  workspace_id UUID NULL REFERENCES workspaces(id) ON DELETE CASCADE,  -- NULL = all workspaces (operator)
  expires_at   TIMESTAMPTZ NULL,
  revoked_at   TIMESTAMPTZ NULL,
  last_used_at TIMESTAMPTZ NULL,
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT uq_service_token UNIQUE (token_hash)
);
CREATE TRIGGER tg_touch_service_tokens BEFORE UPDATE ON service_tokens
  FOR EACH ROW EXECUTE FUNCTION trg_touch_updated_at();
```

- The CLI (service-routed per X.2 — never direct DB) authenticates with a bearer `service_token`; `operator` role reaches the admin endpoints, `readonly` the inspection ones; every use stamps `last_used_at` and operator mutations audit as `actor_kind='operator'`, `channel='cli'`.
- Issuance/revocation is an operator action in the web admin surface (or the bootstrap runbook for the first token: an INSERT in the break-glass session).
- **Degraded operation:** if the API is down the CLI is down — accepted; the break-glass psql runbook (§5) is the only bypass, and DB-down means the runbook's target is gone too, which is the DR plan's territory (`05` §DR), not an auth question.

## §6b. Auth-plane RLS DDL (pass 5 — this file's tables; the `02` §7-DDL conventions apply)

The three tables above are the auth-plane class `02` §7 describes: no tenant policies — they are the door tenant context walks through — and exactly two principals: `svc_ingress` (the runtime reader/writer) and `svc_maintenance` (the sweep-door owner; its rows reach it only through `fn_auth_plane_sweep`). Printed here because replay order is file order and these tables are created in this file:

```sql
ALTER TABLE session_tokens ENABLE ROW LEVEL SECURITY;
ALTER TABLE oauth_states   ENABLE ROW LEVEL SECURITY;
ALTER TABLE service_tokens ENABLE ROW LEVEL SECURITY;
GRANT SELECT, INSERT, UPDATE ON session_tokens, oauth_states, service_tokens TO svc_ingress;
GRANT SELECT, DELETE ON session_tokens, oauth_states TO svc_maintenance;
CREATE POLICY p_auth_ingress_sessions ON session_tokens FOR ALL TO svc_ingress
  USING (true) WITH CHECK (true);
CREATE POLICY p_auth_ingress_states   ON oauth_states   FOR ALL TO svc_ingress
  USING (true) WITH CHECK (true);
CREATE POLICY p_auth_ingress_svctok   ON service_tokens FOR ALL TO svc_ingress
  USING (true) WITH CHECK (true);
CREATE POLICY p_auth_sweep_sessions ON session_tokens FOR ALL TO svc_maintenance
  USING (true) WITH CHECK (true);
CREATE POLICY p_auth_sweep_states   ON oauth_states   FOR ALL TO svc_maintenance
  USING (true) WITH CHECK (true);
-- service_tokens has NO sweep policy and svc_maintenance holds no grant on it: issuance and
-- revocation are operator actions (§6), rows are few, and no retention class exists — a sweep
-- door privilege nobody's schedule drives would be an unreviewed capability.
```

## §7. Where each piece lands (increment index)

| Mechanism | Increment |
|---|---|
| oauth_states + reconnect binding | L.6 (ships with Instagram Login); signin/link widening + cookie-nonce CSRF + start-token door at X.3 |
| MultiFernet ring + reencrypt job | L.6 (ring), runbook standing; job kind exists from L.2 |
| audit grants + GUC-required trigger | L.1 (ledger create) |
| retention sweep + archive export | S.4 (with `05` retention table) |
| Google OIDC sign-in + session_tokens + invitations flow | X.3 (sign-up without Telegram); acceptance runs through `fn_invitation_accept` (`02` §7-DDL) |
| EmailSender port + Resend default + bounce webhook | X.3 (the `send_email` job kind exists from L.2's registry; the `email_global` budget scope from L.2) |
| rate_counters pre-auth scope (`preauth_ip`) | schema at L.2 (`02` §6); consumed here at X.3 |
| `archive` schema (audit exports + M.3 snapshots) | created by the `02` §7-DDL block (F.2 schema landing); M.3 snapshot tables ALTER OWNER to svc_maintenance; exports live from S.4; access rules §4 |
| service_tokens + CLI routing | X.2 (pass 5 — relocated from the deleted W.6) |
| hygiene ratchet patterns (provider_account_ref out of logs) | F.6 (second pattern list on the same ratchet) |

## §8. Intent self-transition guard (integrity, not auth — #883)

**This block is not a security-model object, and its placement here is mechanical.** The advertised
stream is the normative blocks of `02` then `07` in file order, and the target lineage must remain an
*ordered positional prefix* of it. `060` completed F.2 and made the two equal, so the only place a new
statement can land without displacing every `07` statement relative to its migration file is the end of
the last doc. `02` §5 carries the pointer; this is where the SQL lives. If a later increment adds a
third normative doc, this belongs in it.

**What it fixes.** `trg_intent_guard` compares only under `NEW.state IS DISTINCT FROM OLD.state`, so a
same-state write skips every check and succeeds as a no-op. That is inert in the ledger — no audit row,
`entered_state_at` unmoved — and *not* inert to the caller. Under READ COMMITTED the loser of a
concurrent transition blocks on the row lock, re-evaluates against the winner's committed row, and is
told it succeeded with `rowcount = 1`, identical to the winner. Two callers believe they transitioned;
one did. `rowcount` cannot separate them, so no service-side check recovers the distinction.

**Why a second trigger.** A `BEFORE UPDATE FOR EACH ROW` trigger cannot distinguish "SET state to the
same value" from "did not touch state" — both present as `NEW.state = OLD.state`, which is exactly why
the existing guard is written with `IS DISTINCT FROM`. `UPDATE OF state` fires on the column being
**named in the SET list** whatever its value; that is the discriminator, and checkpoint updates
(`SET ig_permalink = …`) never name `state` and so never fire it.

Postgres fires `BEFORE` row triggers in name order, and `tg_intent_guard` sorts first, so a terminal
row still reports terminal immutability rather than this rule.

```sql
CREATE FUNCTION trg_intent_no_self_transition() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION 'same-state write to post_intent % (state %) — a transition that did not happen', OLD.id, OLD.state
    USING ERRCODE = 'check_violation';
END $$;

CREATE TRIGGER tg_intent_no_self_transition BEFORE UPDATE OF state ON post_intents
  FOR EACH ROW WHEN (NEW.state IS NOT DISTINCT FROM OLD.state)
  EXECUTE FUNCTION trg_intent_no_self_transition();
```

Raising the transaction isolation level would also close this — at REPEATABLE READ and above the loser
gets a serialization error — and is deliberately **not** the fix here: it is a fleet-wide change to
every transaction L.0 opens, with its own retry semantics, and wants its own decision.

## §9 — The reauth-prompt clock leg (#942 W5e; lands as migration 062)

`02` §5 :1165 declares `reauth_prompt` with the clock as its producer; nothing implemented the
leg, so the kind had executors' worth of design and no mints. This block is the leg, its marker
column, the column-scoped grant 057's matrix style requires, and the fn_clock_tick replace —
bracketed with 059's transient CREATE grant, because ALTER FUNCTION … OWNER TO needs the new
owner to hold CREATE on the schema.

```sql
-- Migration 062: the reauth-prompt clock leg + the marker it keys on (W5e half
-- of the credential lifecycle; `02` §5 :1165 declared the leg, nothing
-- implemented it — reauth_prompt had NO producer anywhere).
--
-- Also the arming index note: `store_credential` now arms `next_refresh_at`
-- at store time (Python side, same change set) — without that, a credential
-- stored at reconnect is invisible to the refresh leg forever, which is the
-- second pin of the silent-death landmine this change set pulls.
--
-- The cadence is `05`'s operational number (reauth-prompt cadence: 1 prompt /
-- account / week) and is deliberately NOT a parameter: the tick's signature
-- stays stable, and the number's home is `05`, not a config knob.

ALTER TABLE ig_accounts
  ADD COLUMN last_reauth_prompt_at TIMESTAMPTZ NULL;

COMMENT ON COLUMN ig_accounts.last_reauth_prompt_at IS
  'When the reauth-prompt clock leg last minted a prompt for this account. '
  'NULL = never prompted. Stamped at MINT (not delivery), symmetric with the '
  'refresh leg re-arming next_refresh_at at mint.';

CREATE INDEX ix_ig_accounts_reauth_due
  ON ig_accounts (last_reauth_prompt_at)
  WHERE state = 'reauth_required';

-- 057's matrix gives svc_clock full-table SELECT on ig_accounts and column
-- UPDATE on exactly next_slot_at (:129); the new marker needs the same
-- column-scoped shape:
GRANT UPDATE (last_reauth_prompt_at) ON ig_accounts TO svc_clock;

-- The return shape gains o_reauth_jobs, so this is DROP + CREATE (CREATE OR
-- REPLACE cannot change a RETURNS TABLE shape). Owner/grants re-established
-- below, matching 059's posture exactly — including its TRANSIENT, BRACKETED
-- CREATE grant: ALTER FUNCTION … OWNER TO needs the new owner to hold CREATE
-- on the schema (membership alone fails with "permission denied for schema
-- public" — 059's own words, re-confirmed by this migration failing exactly
-- that way without the bracket). Granted here, revoked below; the
-- steady-state matrix never carries CREATE for a door owner.
GRANT CREATE ON SCHEMA public TO svc_clock;

DROP FUNCTION fn_clock_tick(int, interval, jsonb);

CREATE FUNCTION fn_clock_tick(p_max int, p_refresh_cadence interval,
                              p_recurring jsonb)  -- {v:1, "<kind>": seconds, …} (05 seam)
RETURNS TABLE (o_slot_jobs int, o_refresh_jobs int, o_sync_jobs int,
               o_recurring_jobs int, o_reauth_jobs int)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE k text; cadence interval; last_done timestamptz; rem int;
        n1 int := 0; n2 int := 0; n3 int := 0; n4 int := 0; n5 int := 0;
BEGIN
  PERFORM set_config('app.actor_kind', 'clock', true);
  -- (1) recurring system singletons: if no ready/leased row holds the kind's singleton key,
  -- insert the next run at last-completion + cadence (or now, whichever is later):
  FOR k, cadence IN
    SELECT key, (value::text)::numeric * interval '1 second'
      FROM jsonb_each(p_recurring) WHERE key <> 'v'
  LOOP
    EXIT WHEN n4 >= p_max;
    IF NOT EXISTS (SELECT 1 FROM jobs
                   WHERE kind = k AND state IN ('ready','leased')) THEN
      SELECT max(updated_at) INTO last_done FROM jobs
       WHERE kind = k AND state = 'succeeded';
      INSERT INTO jobs (kind, workspace_id, lane, serialization_key, run_at, max_attempts, payload)
      VALUES (k, NULL, 'bulk', k,            -- system singletons key on their kind (§5 registry)
              GREATEST(now(), COALESCE(last_done + cadence, now())), 3,
              jsonb_build_object('v', 1));
      n4 := n4 + 1;
    END IF;
  END LOOP;
  rem := GREATEST(p_max - n4, 0);            -- the running remainder every later leg draws on;
                                             -- each leg's LIMIT keeps its count ≤ rem, so the
                                             -- plain subtractions below cannot go negative
  -- (2) due accounts → plan_slot jobs + slot-cursor advance, one set-based statement
  -- (the O(due) scan, H3; ix_ig_accounts_due serves it):
  WITH due AS (
    SELECT a.id, a.workspace_id, a.next_slot_at,
           COALESCE(a.tz, w.tz)                                   AS eff_tz,
           COALESCE(a.posts_per_day, w.posts_per_day)             AS eff_ppd,
           COALESCE(a.posting_hours_start, w.posting_hours_start) AS eff_start,
           COALESCE(a.posting_hours_end, w.posting_hours_end)     AS eff_end
      FROM ig_accounts a JOIN workspaces w ON w.id = a.workspace_id
     WHERE a.state = 'active' AND a.next_slot_at IS NOT NULL AND a.next_slot_at <= now()
       AND w.state = 'active' AND NOT w.is_paused
     ORDER BY a.next_slot_at LIMIT rem
  ), ins AS (
    INSERT INTO jobs (kind, workspace_id, lane, serialization_key, run_at, max_attempts, payload)
    SELECT 'plan_slot', d.workspace_id, 'bulk', 'acct:' || d.id, now(), 3,
           jsonb_build_object('v', 1, 'ig_account_id', d.id, 'slot_at', d.next_slot_at)
      FROM due d
  )
  UPDATE ig_accounts a
     SET next_slot_at = fn_next_slot(d.next_slot_at, d.eff_tz, d.eff_start, d.eff_end, d.eff_ppd)
    FROM due d WHERE a.id = d.id;
  GET DIAGNOSTICS n1 = ROW_COUNT;
  rem := rem - n1;
  -- (3) due credential refreshes — one set-based statement (D31: the scheduled refresh is also
  -- the liveness probe; the cadence is decoupled from expiry proximity). Reads ride svc_clock's
  -- payload-free column grant; ix_credentials_refresh_due serves the scan:
  WITH due AS (
    SELECT id, workspace_id FROM oauth_credentials
     WHERE state = 'active' AND next_refresh_at IS NOT NULL AND next_refresh_at <= now()
     LIMIT rem
  ), ins AS (
    INSERT INTO jobs (kind, workspace_id, lane, serialization_key, run_at, max_attempts, payload)
    SELECT 'refresh_credential', d.workspace_id, 'bulk', 'cred:' || d.id, now(), 5,
           jsonb_build_object('v', 1, 'credential_id', d.id)
      FROM due d
  )
  UPDATE oauth_credentials c SET next_refresh_at = now() + p_refresh_cadence
    FROM due d WHERE c.id = d.id;
  GET DIAGNOSTICS n2 = ROW_COUNT;
  rem := rem - n2;
  -- (4) due source syncs — same shape (H4's slow jittered baseline; pre-slot/demand syncs are
  -- produced by their own sites — the tick owns only the baseline). ix_sources_sync_due serves it:
  WITH due AS (
    SELECT id, workspace_id FROM media_sources
     WHERE state = 'active' AND next_sync_at IS NOT NULL AND next_sync_at <= now()
     LIMIT rem
  ), ins AS (
    INSERT INTO jobs (kind, workspace_id, lane, serialization_key, run_at, max_attempts, payload)
    SELECT 'sync_media_source', d.workspace_id, 'bulk', 'src:' || d.id, now(), 5,
           jsonb_build_object('v', 1, 'source_id', d.id, 'reason', 'baseline')
      FROM due d
  )
  UPDATE media_sources s SET next_sync_at = NULL                   -- the sync executor re-arms it
    FROM due d WHERE s.id = d.id;
  GET DIAGNOSTICS n3 = ROW_COUNT;
  rem := rem - n3;
  -- (5) reauth prompts for accounts sitting reauth_required (`02` §5 :1165; `05`: 1/week).
  -- Marker stamped at MINT, symmetric with legs 2-4's re-arm-at-mint shape; the NOT EXISTS
  -- guards a still-open prompt job so a slow executor cannot pile up prompts for one account.
  -- ix_ig_accounts_reauth_due serves the scan:
  WITH due AS (
    SELECT a.id, a.workspace_id, a.provider_account_ref
      FROM ig_accounts a
     WHERE a.state = 'reauth_required'
       AND (a.last_reauth_prompt_at IS NULL
            OR a.last_reauth_prompt_at <= now() - interval '7 days')
       AND NOT EXISTS (SELECT 1 FROM jobs j
                        WHERE j.kind = 'reauth_prompt'
                          AND j.serialization_key = 'ig:' || a.provider_account_ref
                          AND j.state IN ('ready','leased'))
     LIMIT rem
  ), ins AS (
    INSERT INTO jobs (kind, workspace_id, lane, serialization_key, run_at, max_attempts, payload)
    SELECT 'reauth_prompt', d.workspace_id, 'bulk', 'ig:' || d.provider_account_ref, now(), 3,
           jsonb_build_object('v', 1, 'ig_account_id', d.id)
      FROM due d
  )
  UPDATE ig_accounts a SET last_reauth_prompt_at = now()
    FROM due d WHERE a.id = d.id;
  GET DIAGNOSTICS n5 = ROW_COUNT;
  RETURN QUERY SELECT n1, n2, n3, n4, n5;
END $$;

ALTER FUNCTION fn_clock_tick(int, interval, jsonb) OWNER TO svc_clock;

REVOKE CREATE ON SCHEMA public FROM svc_clock;

REVOKE ALL ON FUNCTION fn_clock_tick(int, interval, jsonb) FROM PUBLIC;

GRANT EXECUTE ON FUNCTION fn_clock_tick(int, interval, jsonb) TO svc_worker;
```

**063 — the refresh leg's provider guard (#982 prerequisite, #978 disclosure).**
The leg above selects due credentials on `state`/`next_refresh_at` with no
provider filter, and `ig_refresh` builds IG-shaped params against
`graph.instagram.com` unconditionally. That is safe by construction only —
`store_credential` binds `PROVIDER = "ig_login"` and is the single INSERT site —
but `ck_credentials_provider` already admits `'gdrive'`, so the first Drive
credential writer makes it live: the token goes to the wrong host, draws a
definitive 400, and the row is wrongly `mark_dead`-ed (both D31 flips,
permanent until reconnect). The guard makes the coupling explicit in SQL and
fails closed for any provider whose refresh door does not exist yet.

A DROP + CREATE rather than an edit to the block above: the runner keys on
file-byte checksums, so an applied file that changes is a hard failure — fix
forward. Arm (b) is an ordered prefix, so this appends rather than amends.

```sql
-- The CREATE bracket is 062's, carried forward for the same reason it gave:
-- `ALTER FUNCTION … OWNER TO` needs the incoming owner to hold CREATE on the
-- schema, and the steady-state grant matrix never leaves CREATE with a door
-- owner. Granted here, revoked below. Dropping this bracket makes the migration
-- fail at the ALTER — 062 records having failed exactly that way.
GRANT CREATE ON SCHEMA public TO svc_clock;

DROP FUNCTION fn_clock_tick(int, interval, jsonb);

CREATE FUNCTION fn_clock_tick(p_max int, p_refresh_cadence interval,
                              p_recurring jsonb)  -- {v:1, "<kind>": seconds, …} (05 seam)
RETURNS TABLE (o_slot_jobs int, o_refresh_jobs int, o_sync_jobs int,
               o_recurring_jobs int, o_reauth_jobs int)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE k text; cadence interval; last_done timestamptz; rem int;
        n1 int := 0; n2 int := 0; n3 int := 0; n4 int := 0; n5 int := 0;
BEGIN
  PERFORM set_config('app.actor_kind', 'clock', true);
  -- (1) recurring system singletons: if no ready/leased row holds the kind's singleton key,
  -- insert the next run at last-completion + cadence (or now, whichever is later):
  FOR k, cadence IN
    SELECT key, (value::text)::numeric * interval '1 second'
      FROM jsonb_each(p_recurring) WHERE key <> 'v'
  LOOP
    EXIT WHEN n4 >= p_max;
    IF NOT EXISTS (SELECT 1 FROM jobs
                   WHERE kind = k AND state IN ('ready','leased')) THEN
      SELECT max(updated_at) INTO last_done FROM jobs
       WHERE kind = k AND state = 'succeeded';
      INSERT INTO jobs (kind, workspace_id, lane, serialization_key, run_at, max_attempts, payload)
      VALUES (k, NULL, 'bulk', k,            -- system singletons key on their kind (§5 registry)
              GREATEST(now(), COALESCE(last_done + cadence, now())), 3,
              jsonb_build_object('v', 1));
      n4 := n4 + 1;
    END IF;
  END LOOP;
  rem := GREATEST(p_max - n4, 0);            -- the running remainder every later leg draws on;
                                             -- each leg's LIMIT keeps its count ≤ rem, so the
                                             -- plain subtractions below cannot go negative
  -- (2) due accounts → plan_slot jobs + slot-cursor advance, one set-based statement
  -- (the O(due) scan, H3; ix_ig_accounts_due serves it):
  WITH due AS (
    SELECT a.id, a.workspace_id, a.next_slot_at,
           COALESCE(a.tz, w.tz)                                   AS eff_tz,
           COALESCE(a.posts_per_day, w.posts_per_day)             AS eff_ppd,
           COALESCE(a.posting_hours_start, w.posting_hours_start) AS eff_start,
           COALESCE(a.posting_hours_end, w.posting_hours_end)     AS eff_end
      FROM ig_accounts a JOIN workspaces w ON w.id = a.workspace_id
     WHERE a.state = 'active' AND a.next_slot_at IS NOT NULL AND a.next_slot_at <= now()
       AND w.state = 'active' AND NOT w.is_paused
     ORDER BY a.next_slot_at LIMIT rem
  ), ins AS (
    INSERT INTO jobs (kind, workspace_id, lane, serialization_key, run_at, max_attempts, payload)
    SELECT 'plan_slot', d.workspace_id, 'bulk', 'acct:' || d.id, now(), 3,
           jsonb_build_object('v', 1, 'ig_account_id', d.id, 'slot_at', d.next_slot_at)
      FROM due d
  )
  UPDATE ig_accounts a
     SET next_slot_at = fn_next_slot(d.next_slot_at, d.eff_tz, d.eff_start, d.eff_end, d.eff_ppd)
    FROM due d WHERE a.id = d.id;
  GET DIAGNOSTICS n1 = ROW_COUNT;
  rem := rem - n1;
  -- (3) due credential refreshes — one set-based statement (D31: the scheduled refresh is also
  -- the liveness probe; the cadence is decoupled from expiry proximity). Reads ride svc_clock's
  -- payload-free column grant; ix_credentials_refresh_due serves the scan:
  WITH due AS (
    SELECT id, workspace_id FROM oauth_credentials
     WHERE state = 'active' AND next_refresh_at IS NOT NULL AND next_refresh_at <= now()
       AND provider = 'ig_login'   -- 063: see the header. Fails CLOSED for any new provider.
     LIMIT rem
  ), ins AS (
    INSERT INTO jobs (kind, workspace_id, lane, serialization_key, run_at, max_attempts, payload)
    SELECT 'refresh_credential', d.workspace_id, 'bulk', 'cred:' || d.id, now(), 5,
           jsonb_build_object('v', 1, 'credential_id', d.id)
      FROM due d
  )
  UPDATE oauth_credentials c SET next_refresh_at = now() + p_refresh_cadence
    FROM due d WHERE c.id = d.id;
  GET DIAGNOSTICS n2 = ROW_COUNT;
  rem := rem - n2;
  -- (4) due source syncs — same shape (H4's slow jittered baseline; pre-slot/demand syncs are
  -- produced by their own sites — the tick owns only the baseline). ix_sources_sync_due serves it:
  WITH due AS (
    SELECT id, workspace_id FROM media_sources
     WHERE state = 'active' AND next_sync_at IS NOT NULL AND next_sync_at <= now()
     LIMIT rem
  ), ins AS (
    INSERT INTO jobs (kind, workspace_id, lane, serialization_key, run_at, max_attempts, payload)
    SELECT 'sync_media_source', d.workspace_id, 'bulk', 'src:' || d.id, now(), 5,
           jsonb_build_object('v', 1, 'source_id', d.id, 'reason', 'baseline')
      FROM due d
  )
  UPDATE media_sources s SET next_sync_at = NULL                   -- the sync executor re-arms it
    FROM due d WHERE s.id = d.id;
  GET DIAGNOSTICS n3 = ROW_COUNT;
  rem := rem - n3;
  -- (5) reauth prompts for accounts sitting reauth_required (`02` §5 :1165; `05`: 1/week).
  -- Marker stamped at MINT, symmetric with legs 2-4's re-arm-at-mint shape; the NOT EXISTS
  -- guards a still-open prompt job so a slow executor cannot pile up prompts for one account.
  -- ix_ig_accounts_reauth_due serves the scan:
  WITH due AS (
    SELECT a.id, a.workspace_id, a.provider_account_ref
      FROM ig_accounts a
     WHERE a.state = 'reauth_required'
       AND (a.last_reauth_prompt_at IS NULL
            OR a.last_reauth_prompt_at <= now() - interval '7 days')
       AND NOT EXISTS (SELECT 1 FROM jobs j
                        WHERE j.kind = 'reauth_prompt'
                          AND j.serialization_key = 'ig:' || a.provider_account_ref
                          AND j.state IN ('ready','leased'))
     LIMIT rem
  ), ins AS (
    INSERT INTO jobs (kind, workspace_id, lane, serialization_key, run_at, max_attempts, payload)
    SELECT 'reauth_prompt', d.workspace_id, 'bulk', 'ig:' || d.provider_account_ref, now(), 3,
           jsonb_build_object('v', 1, 'ig_account_id', d.id)
      FROM due d
  )
  UPDATE ig_accounts a SET last_reauth_prompt_at = now()
    FROM due d WHERE a.id = d.id;
  GET DIAGNOSTICS n5 = ROW_COUNT;
  RETURN QUERY SELECT n1, n2, n3, n4, n5;
END $$;

COMMENT ON FUNCTION fn_clock_tick(int, interval, jsonb) IS
  'The scheduled clock tick: a SECURITY DEFINER producer of due work, owned by '
  'svc_clock with EXECUTE granted to svc_worker, and pinned to '
  'search_path = pg_catalog, public. One call runs five legs in order - '
  'recurring system singletons, due account slots, due credential refreshes, '
  'due source syncs, and reauth prompts. The legs share one budget: p_max caps '
  'the first, and each later leg draws only on what the ones before it left, so '
  'one call mints at most p_max rows. It is NOT the only writer of jobs - '
  'application services enqueue directly as well. The refresh leg is scoped to '
  'provider ig_login and fails closed: a provider with no refresh door of its '
  'own is skipped rather than minted.';

ALTER FUNCTION fn_clock_tick(int, interval, jsonb) OWNER TO svc_clock;

REVOKE CREATE ON SCHEMA public FROM svc_clock;

REVOKE ALL ON FUNCTION fn_clock_tick(int, interval, jsonb) FROM PUBLIC;

GRANT EXECUTE ON FUNCTION fn_clock_tick(int, interval, jsonb) TO svc_worker;
```

### §10. The memberships door — the tenth `02` §7 door (064, #1037)

The web surface's first read after sign-in is "which workspaces am I in", and
the printed policies cannot answer it: `p_tenant` on `workspace_members` is
`workspace_id = app.tenant_id`, so with no tenant claimed the table reads
empty — an answer indistinguishable from the greenfield's normal
signed-in-with-no-workspace state. Found twice, independently (#1031 on the
writer side, #1035 on the router side), confirmed one gap, and both lanes
refused rather than answered `[]` until this door existed. It reads the
caller from `app.actor_user_id` internally rather than taking a parameter
(one trust point — the GUC the audit triggers already rely on — and unset
fails closed), and it carries the `(user_id)` index the read was missing.
Appends rather than amends: arm (b) is an ordered prefix, and the `02` §7-DDL
block that prints the first nine doors is content-addressed.

```sql
-- 10/10 fn_memberships_for_caller — the user-plane door the web surface reads its workspace
-- list through (#1037; the gap was found twice — #1031 writer-side, #1035 router-side — and
-- confirmed one gap). p_tenant on workspace_members is workspace_id = app.tenant_id, so "which
-- workspaces does this user belong to" has no pre-context read path for svc_ingress: with no
-- tenant set the table reads EMPTY, and an empty list is indistinguishable from the greenfield's
-- normal signed-in-with-no-workspace state. Both lanes therefore REFUSED
-- (membership_list_unreadable) until this door existed. The caller is read from
-- app.actor_user_id INTERNALLY, never taken as a parameter: one trust point — the GUC the
-- audit triggers already attribute every governance write to — and an unset GUC fails closed
-- to zero rows. Owned by svc_membership (already USING (true) on workspace_members and SELECT
-- on workspaces); it returns exactly the four columns the surface renders, never a widened read.
-- The CREATE bracket is 062's, for the reason it gave: ALTER FUNCTION … OWNER TO needs the
-- incoming owner to hold CREATE on the schema, and the steady state never leaves it there.
GRANT CREATE ON SCHEMA public TO svc_membership;

CREATE FUNCTION fn_memberships_for_caller()
RETURNS TABLE (o_workspace_id uuid, o_name varchar, o_state text, o_role text)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
  SELECT m.workspace_id, w.name, w.state, m.role
    FROM workspace_members m JOIN workspaces w ON w.id = m.workspace_id
   WHERE m.user_id = NULLIF(current_setting('app.actor_user_id', true), '')::uuid
   ORDER BY w.created_at, w.id
$$;

COMMENT ON FUNCTION fn_memberships_for_caller() IS
  'The calling user''s workspace memberships: a SECURITY DEFINER user-plane read owned by '
  'svc_membership with EXECUTE granted to svc_ingress. The caller is app.actor_user_id, read '
  'internally and never a parameter; unset, it returns no rows. Exists because p_tenant on '
  'workspace_members cannot answer a cross-tenant question for one user.';

ALTER FUNCTION fn_memberships_for_caller() OWNER TO svc_membership;

REVOKE CREATE ON SCHEMA public FROM svc_membership;

REVOKE ALL ON FUNCTION fn_memberships_for_caller() FROM PUBLIC;

GRANT EXECUTE ON FUNCTION fn_memberships_for_caller() TO svc_ingress;

-- The read the door serves had no index: workspace_members' primary key is (workspace_id,
-- user_id), so a by-user lookup was a sequential scan.
CREATE INDEX ix_members_user ON workspace_members (user_id);
```

### §11. `alert_stranded_sources` joins the job-kind vocabulary (065, #1061)

A media source that fails persistently is set to `error` and alerted once.
Recovery to `active` happens only on a successful sync, and `fn_clock_tick`'s
leg 4 enqueues only `active` sources — so the source is never scheduled again,
the branch that alerts never runs again, and no second alert ever fires. The
stuckness was causing the silence, and a workspace whose Drive access was
revoked saw one message and then nothing, indistinguishable from health. The
remedy is a recurring system singleton that re-alerts on a bound, which needs a
job kind; re-arming the source is fork F4 (a) and belongs to the connect flow.

**Two constraints gate that insert, not one.** `ck_jobs_kind` is the vocabulary.
`ck_jobs_system_kinds` is a biconditional — `(workspace_id IS NULL) = (kind IN
<system kinds>)` — and the recurring leg inserts singletons with `workspace_id
NULL`, so widening the vocabulary alone leaves the row refused. Both are widened
and the kind goes in the system group in both: this beat is one row that sweeps
every workspace, the `reap_expired` shape rather than the `sync_media_source`
shape.

**The failure that would otherwise follow is total and fails first.** The
recurring mint is leg 1 of five inside one `fn_clock_tick` body with no
`EXCEPTION` section, so it opens no subtransaction — an unhandled error aborts
every leg, and slot minting, credential refreshes, source syncs and reauth
prompts all stop. Widening a CHECK is safe on existing rows by construction: any
row satisfying the narrower predicate satisfies the wider one. Appends rather
than amends, for the same reason §10 did — the `02` §5 machinery block that
prints these constraints is content-addressed and arm (b) is an ordered prefix.

```sql
-- alert_stranded_sources (#1061): a system singleton that re-alerts sources stranded in
-- `error`. BOTH constraints gate the insert — the vocabulary and the workspace_id
-- biconditional — so both are widened, and the kind joins the SYSTEM group in each.
-- fn_clock_tick is deliberately untouched: fork F4 rejected widening what the tick SELECTS,
-- and adding an allowed VALUE to a constraint is a different act from changing which rows
-- the tick selects.
ALTER TABLE jobs DROP CONSTRAINT ck_jobs_kind;
ALTER TABLE jobs ADD CONSTRAINT ck_jobs_kind CHECK (kind IN (
  -- tenant kinds (workspace_id NOT NULL):
  'plan_slot','publish_pipeline','deliver_outbox','sync_media_source',
  'first_ingest_chunk','refresh_credential','offboard_workspace',
  'revoke_workspace_credentials','reauth_prompt',
  -- system kinds (workspace_id NULL):
  'reconcile_ambiguous','reap_expired','reap_transit_assets','retention_sweep',
  'reencrypt_credentials','send_email','alert_stranded_sources'));

ALTER TABLE jobs DROP CONSTRAINT ck_jobs_system_kinds;
ALTER TABLE jobs ADD CONSTRAINT ck_jobs_system_kinds CHECK (
  (workspace_id IS NULL) = (kind IN
    ('reconcile_ambiguous','reap_expired','reap_transit_assets','retention_sweep',
     'reencrypt_credentials','send_email','alert_stranded_sources')));
```

### §12. The "no media available" notice marker (066, #1090 D3)

`06` §5's slot-missed row promises that "a 'no media available' notification
fires at most once per `05` window when selection returns empty", and names the
mechanism as "slot planner + notification dedup". The slot planner shipped; the
dedup had nowhere to live, so neither did the notice — a grep for a no-media
notification returned nothing anywhere in the tree, against a positive control
that does enumerate the live outbox kinds. This is the marker half; the producer
that stamps it is `scheduler.execute_plan_slot`, which already owns the empty
case (`02` §5 names "no media" as its outcome).

**Keyed per account, not per workspace.** A slot is minted per (workspace,
`ig_account`) and the cadence lives on `ig_accounts`, so two accounts in one
workspace starve independently. A workspace-keyed marker would let the first
account's notice silence the second account's *first* notice — the failure the
line exists to rule out is "you are told once", not "one of your accounts is
told once".

**No index, and the contrast with §9 is the reason.** §9 added
`ix_ig_accounts_reauth_due` because the clock SCANS `ig_accounts` for accounts
whose prompt is due — a predicate over the whole table. This marker is only ever
read for the one account a `plan_slot` job already names, by primary key, so an
index would be dead weight on every write serving no query that exists.

**No grant, same shape of contrast.** §7's matrix gives `svc_worker` table-level
`SELECT, INSERT, UPDATE` on `ig_accounts`, so the role that stamps this reaches
it already. §9 needed an explicit column grant because `svc_clock`'s grant is
column-scoped (`next_slot_at` and nothing else); the clock does not touch this
marker, and adding a column grant would widen `svc_clock`'s reach for no caller.

Appends rather than amends, for the same reason §10 and §11 did — the `02` §2
block that prints `ig_accounts` is content-addressed and arm (b) is an ordered
prefix.

```sql
-- The no-media notice dedup marker (#1090 D3). The window itself (05: 24 h) is a
-- WorkerConfig parameter, not a constant here: the column records WHEN the notice
-- last fired, never HOW OFTEN it may.
ALTER TABLE ig_accounts
  ADD COLUMN last_no_media_notice_at TIMESTAMPTZ NULL;

COMMENT ON COLUMN ig_accounts.last_no_media_notice_at IS
  'When this account last told its workspace that a slot found no media. '
  'NULL = never told. Stamped by the slot planner at NOTICE time, in the same '
  'transaction as the outbox row, so a rolled-back plan takes its notice with '
  'it. The dedup window is 05 (24 h) and lives in WorkerConfig, not here: the '
  'column records WHEN, never HOW OFTEN.';
```

### §13. The `bind` purpose: a Telegram group joins a workspace by a one-shot link (067, #1175 D-3/D-4)

The plan ratified `0..n` Telegram bindings per workspace (D13) and never said
how a binding is created; `bindings.bind` existed with no caller. The owner's
ruling (2026-09-05): **a token from Settings.** An admin presses *Add a Telegram
group*; the site mints `t.me/<bot>?startgroup=bind-<state>`, which opens
Telegram's group picker, adds the bot to the chosen group and sends
`/start bind-<state>` there; the `/start` door consumes the state one-shot and
binds THAT chat to the pinned workspace. The same flow binds the second and the
tenth group. The link is admin-floored at issue, one live link per workspace
(issuing retires the workspace's earlier live bind states), and a group another
workspace already holds is refused silently — `uq_binding_external` decides,
and the router's existence-oracle rule keeps the refusal mute.

```sql
-- The bind purpose (#1175 D-3, owner ruling 2026-09-05): an admin's one-shot
-- `startgroup` link binds the group it is opened in to the pinned workspace.
-- `ck_oauth_state_context`'s ELSE branch already requires BOTH user_id and
-- workspace_id for any purpose that is not signin or link, which is exactly
-- what a bind state must pin. Drop-and-add is the repo's shape for a CHECK
-- edit (042, 045, 046, 049, 065).
ALTER TABLE oauth_states DROP CONSTRAINT ck_oauth_state_purpose;
ALTER TABLE oauth_states ADD CONSTRAINT ck_oauth_state_purpose
  CHECK (purpose IN ('connect','reconnect','signin','link','bind'));
```
