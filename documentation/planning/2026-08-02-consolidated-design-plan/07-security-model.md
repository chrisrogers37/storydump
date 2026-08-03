# Security model

The auth, secrets, and integrity designs review A §5.10–14 found missing. Same DDL conventions as `02` §0. Everything here is v1-executable; each mechanism names its increment in `04`.

## §1. Web sign-in: email OTP + sessions (X.3; session infrastructure shared with W.6's web surfaces)

```sql
CREATE TABLE otp_challenges (
  id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  email        TEXT NOT NULL,                   -- lowercased
  code_hash    TEXT NOT NULL,                   -- argon2id of the 6-digit code; plaintext never stored
  purpose      TEXT NOT NULL DEFAULT 'signin'
               CONSTRAINT ck_otp_purpose CHECK (purpose IN ('signin','email_change')),
  attempts     INTEGER NOT NULL DEFAULT 0,      -- verify attempts; hard cap then dead (05 seam)
  expires_at   TIMESTAMPTZ NOT NULL,            -- now() + OTP TTL at issue (05 seam)
  consumed_at  TIMESTAMPTZ NULL,
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TRIGGER tg_touch_otp_challenges BEFORE UPDATE ON otp_challenges
  FOR EACH ROW EXECUTE FUNCTION trg_touch_updated_at();
CREATE INDEX ix_otp_live ON otp_challenges (email) WHERE consumed_at IS NULL;

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

- **Issue:** rate-limited per email and per source IP through `rate_counters` (`02` §6 — scopes `otp_email`/`otp_ip`; limits in the `05` pre-auth row; the client-IP source rule is stated once at the `02` §6 table). A mechanism deliberately distinct from the per-workspace S.2 admission (which is fail-closed on tenant context and structurally cannot serve unauthenticated requests). Each issue invalidates prior live challenges for the email (`consumed_at = now()` sweep in the issue transaction) and enqueues the code as a `send_email` job (the EmailSender port below). **The issue response is enumeration-safe (pass 3):** the endpoint returns the same "code sent if eligible" response whether the email is known, unknown, or rate-limited — account existence is never signaled pre-auth.
- **Verify — lock, compare, then consume-on-success (pass 3; R3 review §6.5: the pass-2 form consumed before verifying, so a committed failed attempt burned the challenge and a rolled-back one erased its own attempts increment).** Verbatim flow, one transaction:

```sql
BEGIN;
SELECT code_hash, attempts FROM otp_challenges
 WHERE id = :id AND consumed_at IS NULL AND expires_at > now()
   AND attempts < :otp_max_attempts                    -- 05 seam
 FOR UPDATE;
-- no row ⇒ dead challenge (consumed / expired / attempt-capped): generic failure, nothing written
-- application: argon2id verify against code_hash, then exactly ONE of:
UPDATE otp_challenges SET consumed_at = now()       WHERE id = :id;   -- match: consume
UPDATE otp_challenges SET attempts   = attempts + 1 WHERE id = :id;   -- mismatch: count it
COMMIT;
```

  The row lock serializes concurrent verifies of one challenge; a failed attempt **commits** its increment (never vanishes with a rollback); a success consumes exactly once, with all remaining attempts intact until then. Success then upserts `user_identities(provider='email_otp', external_id=email, verified_at=now())` (creating the `users` row on first sign-in) and issues a session.
- **Sessions:** opaque random 256-bit value in an httpOnly/SameSite=Lax/secure cookie; only the hash is stored; verification is one indexed lookup + expiry/revocation check; sliding renewal. Sign-out and admin revoke set `revoked_at`. There is no JWT for human web sessions — JWTs exist only where W.6's consumer contract already has them (BFF/API), gaining the additive `workspace_id` claim there.
- **Recovery:** none beyond OTP itself — the email **is** the factor. Email change = OTP challenge with `purpose='email_change'` against the *new* address from an authenticated session, then the identity's `external_id` swaps in one audited transaction. Losing the mailbox loses the account (v1 posture, documented; Telegram-identity users are unaffected — different provider row).

**Email delivery — the `EmailSender` port (pass 3; R3 review §5.1: as specified, X.3 could not deliver its own sign-in codes — challenge storage existed, delivery did not).** One port: `send(to, template, params) → provider_message_ref`, drained by `send_email` jobs (`02` §5 registry — interactive lane, because a sign-in user is waiting; payload carries everything, no tenant reads at send time). **Named default provider: Resend** — no email infrastructure exists in this repository today, the free tier covers our volume, and the integration is one authenticated POST. **A new external service is a flagged decision, not an assumption: the manager/owner ack is requested and recorded in `03` (pass-3 owner items); the port keeps the provider swappable if the ack lands elsewhere.** Retry budget per `05`; a bounce/complaint webhook (ingress-hosted) invalidates the live challenge for that email (`consumed_at = now()`) and audits the event; sender-domain setup (SPF/DKIM/return-path) is X.3's runbook item, and X.3's gate delivers a real code end-to-end.

## §2. OAuth flows: state tokens and reconnect binding (L.6)

```sql
CREATE TABLE oauth_states (
  state         TEXT PRIMARY KEY,               -- 128-bit urlsafe random
  user_id       UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  workspace_id  UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
  provider      TEXT NOT NULL CONSTRAINT ck_oauth_state_provider CHECK (provider IN ('ig_login','gdrive')),
  purpose       TEXT NOT NULL CONSTRAINT ck_oauth_state_purpose CHECK (purpose IN ('connect','reconnect')),
  reconnect_target UUID NULL,                   -- ig_account_id | media_source_id when purpose='reconnect'
  expires_at    TIMESTAMPTZ NOT NULL,           -- now() + state-token TTL (05 seam)
  consumed_at   TIMESTAMPTZ NULL,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TRIGGER tg_touch_oauth_states BEFORE UPDATE ON oauth_states
  FOR EACH ROW EXECUTE FUNCTION trg_touch_updated_at();
```

RLS class: `otp_challenges`, `session_tokens`, `oauth_states`, and `service_tokens` are **auth-plane tables** — role-scoped `USING (true)` policies for `svc_ingress` and the sweep door's NOLOGIN owner (`02` §7), no tenant RLS, because they are the door tenant context walks through (`02` §7 states the class; their expiry/retention classes are `05` rows, and their sweep runs only through `fn_auth_plane_sweep` — the `02` §7 door, whose NOLOGIN owner carries the enumerated auth-plane policies, driven on the reaper/retention schedule).

- Issued only from an authenticated session whose user holds admin+ in `workspace_id` (checked at issue AND at callback — the row pins both, so a callback cannot be replayed into a different workspace).
- Callback consume is one-shot CAS (`… WHERE state = :s AND consumed_at IS NULL AND expires_at > now() RETURNING …`); a consumed/expired/unknown state is rejected cold. CSRF safety comes from the state being unguessable, single-use, and session-bound.
- **Reconnect binding:** `purpose='reconnect'` pins the exact credential owner being replaced; the callback transaction swaps `encrypted_payload` in place (same row id — no window where the account has zero credentials) and flips `ig_accounts.state` `reauth_required → active`. **Concurrent reconnects — "last issued wins", made true of the schema (pass 3; R3 review §6.6: the pass-2 "last consumed wins" claim was false — independently issued state rows never consumed one another, so both callbacks could land):** issuing a reconnect state **invalidates prior live states for the same target in the issue transaction** — `UPDATE oauth_states SET consumed_at = now() WHERE purpose = 'reconnect' AND reconnect_target = :target AND consumed_at IS NULL` — so at most one live state exists per target at any commit; the callback CAS consumes it one-shot, and a superseded state's callback finds it consumed and shows "a newer reconnect superseded this one". Connect-vs-reconnect races on the same account still collapse on `uq_credential_per_account`.

## §3. Credential encryption and key rotation (review A §5.12; L.6 + a standing runbook)

- **Mechanism: MultiFernet** (extends the existing Fernet usage rather than replacing it): env `CREDENTIAL_KEYS` is an ordered list; index 0 encrypts, all entries decrypt. No per-row key-id column — Fernet ciphertext self-identifies against the ring by trial, and the operational question ("which rows still need the old key") is answered by the rotation job's progress, not a column.
- **Rotation runbook:** (1) prepend the new key to `CREDENTIAL_KEYS`, deploy; (2) enqueue `reencrypt_credentials` (system job, `02` §5 registry): batched sweep re-encrypting every row with key 0 (MultiFernet `rotate()`), progress in audit; (3) when the sweep reports zero remaining, remove the retired key from env, deploy. Rotation is therefore two config deploys around one job — no schema change, no downtime, safe to abort mid-way (old key still in ring until step 3).
- **Missing-key failure:** a payload no ring entry decrypts (key removed too early, restored backup from before rotation) fails closed — the credential flips `state='expired'`, the account flips `reauth_required`, the re-auth path recovers it. Never guess, never log ciphertext.
- **Least privilege:** `encrypted_payload` is column-SELECTable only by the roles that must decrypt (`02` §7 grant matrix); the clock schedules refreshes through the payload-free view. Payloads never appear in logs, traces, audit `detail`, or error payloads — the §5 hygiene rule, test-enforced.

## §4. Audit integrity and retention (review A §5.13)

- **Append-only in the database:** no role holds UPDATE on `audit_events`; DELETE only via `svc_maintenance`'s retention sweep (`02` §7). The `02` §4 audit trigger's GUC requirement means every state change carries a named actor — including break-glass psql sessions (below).
- **Retention:** `05` table — audit rows kept 400 days, then swept via `fn_retention_batch` (`02` §7). Before each sweep batch is deleted it is COPY-exported **into the in-database `archive` schema** as a batch table (`05` §DR names the location; the rationale and the properties that decided it are `03` D30); **the sweep aborts if the export fails — export-or-abort, never delete-then-hope**. No login role holds any grant on the `archive` schema: writes happen only inside `svc_maintenance`-owned door bodies and `svc_migration` contract migrations; reads are the break-glass runbook (§5). Aged archive tables are dropped *as tables* per their `05` retention rows. Queryability of archives is explicitly not a v1 feature.
- **Redaction rule:** `detail` JSONB never contains secrets, tokens, OTP codes, or `provider_account_ref` (internal UUIDs only); enforced by the writer helper everything routes through + a test that greps captured audit output in the harness. Tamper evidence beyond grants (hash chains, signed exports) is explicitly not v1 — the stated integrity level is "no role can rewrite history without leaving a grant violation," which is what the grant matrix delivers.

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

## §7. Where each piece lands (increment index)

| Mechanism | Increment |
|---|---|
| oauth_states + reconnect binding | L.6 (ships with Instagram Login) |
| MultiFernet ring + reencrypt job | L.6 (ring), runbook standing; job kind exists from L.2 |
| audit grants + GUC-required trigger | L.1 (ledger create) |
| retention sweep + archive export | S.4 (with `05` retention table) |
| otp_challenges + session_tokens + invitations flow | X.3 (sign-up without Telegram) |
| EmailSender port + Resend default + bounce webhook | X.3 (the `send_email` job kind exists from L.2's registry) |
| rate_counters pre-auth scopes (`otp_email`/`otp_ip`/`preauth_ip`) | schema at L.2 (`02` §6); consumed here at X.3 |
| `archive` schema (audit exports + contract snapshots) | created by the first contract-stage migration that snapshots (`04` ground rules); exports live from S.4; access rules §4 |
| service_tokens + CLI routing | W.6 (consumer contracts) |
| hygiene ratchet patterns (provider_account_ref out of logs) | F.6 (second pattern list on the same ratchet) |
