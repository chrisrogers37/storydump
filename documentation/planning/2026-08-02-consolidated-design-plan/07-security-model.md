# Security model

The auth, secrets, and integrity designs review A §5.10–14 found missing. Same DDL conventions as `02` §0. Everything here is v1-executable; each mechanism names its increment in `04`.

## §1. Web sign-in: Google OIDC + sessions (X.3; session infrastructure shared with W.6's web surfaces)

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
- **Sessions:** opaque random 256-bit value in an httpOnly/SameSite=Lax/secure cookie; only the hash is stored; verification is one indexed lookup + expiry/revocation check; sliding renewal. Sign-out and admin revoke set `revoked_at`. There is no JWT for human web sessions — and none exists anywhere on `main` today (pass-4 anchor: current API auth is HMAC-signed WebApp init-data + signed URL tokens, `src/utils/webapp_auth.py`); the machine/consumer surfaces get the additive `workspace_id` field on their own signed tokens per W.6 (`04`), and first-party service auth is §6's `service_tokens`.
- **Pre-auth rate limiting:** the sign-in endpoints ride the `preauth_ip` scope (`rate_counters`, `02` §6; the `05` pre-auth row; the client-IP source rule is stated once at the `02` §6 table) — a mechanism deliberately distinct from the per-workspace S.2 admission, which is fail-closed on tenant context and structurally cannot serve unauthenticated requests. The OTP-specific scopes died with OTP.
- **Recovery:** account recovery is Google's problem — a strictly stronger posture than pass 3's "losing the mailbox loses the account". Email *change* ceases to exist as a flow: email is a provider claim, not stored credential material. Telegram-identity users are unaffected (different provider row).
- **Linking (D35 — explicit-only, stated once here):** identities attach to a user only through an action performed inside that user's authenticated session — §2's `link` purpose covers both directions (Telegram-first → Google via OAuth redirect; Google-first → Telegram via the start-token transport). No email auto-merge exists, in any direction; a (provider, subject) already attached to another user rejects with "already linked elsewhere" (`uq_identity_per_provider`); merging two populated users is an operator action with an audit trail, explicitly out of v1.

**Email delivery — the `EmailSender` port (consumer: invitations — FC-6).** One port: `send(to, template, params) → provider_message_ref`, drained by `send_email` jobs (`02` §5 registry — interactive lane: the inviter is mid-flow awaiting send confirmation; payload carries everything, no tenant reads at send time). **Named default provider: Resend** — no email infrastructure exists in this repository today and the integration is one authenticated POST. **The volume claim, quantified (pass 5 — R4 finding):** post-FC-5/FC-6, email = invitations + bounce notices only; the free tier is 100/day and 3,000/month **with sending paused at quota** — launch volume is a rounding error against that, but a cohort-onboarding burst (tens of workspaces × a few invites in a day) crosses 100/day exactly when the product is succeeding, and a paused sender silently strands invitation delivery. The `05` email budget row + the `rate_counters` `email_global` scope (`02` §6) exist so the app defers under its own budget instead of tripping the provider's pause; the input row in `05` names the volume model. **A new external service is a flagged decision, not an assumption: the owner ack is OPEN (`03` pass-4 items — reopened by FC-6 after the sign-in ruling had briefly mooted it); the port keeps the provider swappable until it lands.** Retry budget per `05`. The bounce/complaint webhook (ingress-hosted) now targets invitations: a bounced invitation email writes an audit event and notifies the inviter via an outbox `notification` on the workspace's binding — the pass-3 behavior (invalidating a live challenge) retired with the challenges themselves. Sender-domain setup (SPF/DKIM/return-path) is X.3's runbook item, and X.3's gate delivers a real invitation email end-to-end.

## §2. OAuth flows: state tokens, sign-in/link states, and reconnect binding (L.6; signin/link widening at X.3)

One state machine serves four purposes. The pass-3 table could not serve sign-in structurally (`user_id`/`workspace_id` NOT NULL, session-bound issuance) — FC-5 **widens** it rather than growing a parallel table: purposes gain `signin`/`link`, context nullability becomes purpose-conditional, and the one thing session binding used to provide (CSRF) gets a purpose-appropriate replacement for the anonymous case.

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
- **Least privilege:** `encrypted_payload` is column-SELECTable only by the roles that must decrypt (`02` §7 grant matrix); the clock schedules refreshes through the payload-free view. Payloads never appear in logs, traces, audit `detail`, or error payloads — the §5 hygiene rule, test-enforced.

## §4. Audit integrity and retention (review A §5.13)

- **Append-only in the database:** no role holds UPDATE on `audit_events`; DELETE only via `svc_maintenance`'s retention sweep (`02` §7). The `02` §4 audit trigger's GUC requirement means every state change carries a named actor — including break-glass psql sessions (below).
- **Retention:** `05` table — audit rows kept 400 days, then swept via `fn_retention_batch` (`02` §7). Before each sweep batch is deleted it is COPY-exported **into the in-database `archive` schema** as a batch table (`05` §DR names the location; the rationale and the properties that decided it are `03` D30); **the sweep aborts if the export fails — export-or-abort, never delete-then-hope**. No login role holds any grant on the `archive` schema: writes happen only inside `svc_maintenance`-owned door bodies and `svc_migration` contract migrations; reads are the break-glass runbook (§5). Aged archive tables are dropped *as tables* per their `05` retention rows. Queryability of archives is explicitly not a v1 feature.
- **Redaction rule:** `detail` JSONB never contains secrets, tokens, invitation-token values, or `provider_account_ref` (internal UUIDs only); enforced by the writer helper everything routes through + a test that greps captured audit output in the harness. Tamper evidence beyond grants (hash chains, signed exports) is explicitly not v1 — the stated integrity level is "no role can rewrite history without leaving a grant violation," which is what the grant matrix delivers.

## §5. Existence-oracle and log hygiene (review A §5.14)

The deliberate cross-tenant keys are inventoried in `02` §7 (three, with per-key leak analysis; the material one — `uq_publish_exclusive` — is swallowed into the defer path and never surfaces to a user). The hygiene rule that keeps the rest of the output surface oracle-free:

- Logs, metrics labels, user-visible errors, and audit `detail` reference internal UUIDs (`ig_accounts.id`, `workspaces.id`) — never `provider_account_ref`, handles being user-chosen display data are fine.
- Provider identifiers appear exactly twice in the system's output surface: inside `oauth_credentials` (encrypted) and inside provider adapter calls. A grep-shaped CI check (F.6 ratchet mechanism, second pattern list) holds `provider_account_ref` out of logging call sites.
- Admin/operator queries run through the audited surfaces (§7 of `06`); the break-glass psql runbook requires `SET app.actor_kind = 'operator'` (the audit trigger refuses anonymous writes) and is itself logged at the Neon level.

## §6. First-party API auth for CLI and operator surfaces (review A §3.31; W.6)

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

- The CLI (service-routed per W.6 — never direct DB) authenticates with a bearer `service_token`; `operator` role reaches the admin endpoints, `readonly` the inspection ones; every use stamps `last_used_at` and operator mutations audit as `actor_kind='operator'`, `channel='cli'`.
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
