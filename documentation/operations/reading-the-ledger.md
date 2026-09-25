# Reading the ledger with `storydump`

The questions the last two days of production validation answered with fifty-five one-off SQL
probes are eight `storydump` verbs (the v2 CLI plan, phase 02). Each is one bounded,
tenant-scoped read through the API — never a database connection — under your token, for every
workspace you belong to (or one, with `--workspace`). `psql` through Railway stays the escape
hatch for a question these do not answer; the probes the verbs were built from are kept at
`documentation/archive/2026-09-15-cli-v2/probes/`.

Sign in once (`storydump login`, a token minted under Settings › API tokens). Add `--json` to
any verb for one envelope `{"v": 1, "kind", "data", "error"}` — a workspace read's `data` is
`{"workspaces": [{"workspace_id", "rows"}]}`, `posture`'s is the view's own object; add
`--workspace <id or exact name>` to read one workspace (`posture` takes neither).

| Question | Verb | Rows |
|---|---|---|
| What happened to this story, in order? | `storydump story <intent_id>` | one row: the intent, its audit rows (the `cli_command` rows included), its provider operations (each container permit with the url variant and Meta's answer), its cards |
| Which cards does this story have, on which chats? | `storydump cards <intent_id>` | one per outbox row, adopted twins included, in send order |
| What is floating — approved, carrying a debit, waiting between attempts? | `storydump floating [--limit N]` | one per story with the job that retries it and the last wait's class and rung |
| Where is this account against its cap today? | `storydump account <handle or id>` | cap per day, zone, next slot, today's bucket, the last twenty outcomes |
| What is the job queue doing? | `storydump jobs [--since 3h]` | one per kind × lane × state with the oldest runnable and failed samples |
| What is still owed or lost on the chats? | `storydump outbox [--since 3h]` | pending, sending, ambiguous and failed rows by binding |
| What did the burst do? | `storydump burst [--since 3h]` | one timeline: taps, permits, float waits, siblings posting past a waiter, review cards, and the window's outcome counts |
| What is the database's posture? | `storydump posture` | the migration ledger, the connected role and whether it bypasses RLS, the tables under RLS, the SECURITY DEFINER census |

`--since` takes `45m`, `3h`, `2d`, an ISO-8601 timestamp (`2026-09-15T14:50:00Z`; a naive one
is read as UTC) or a bare date (its midnight UTC); a window is at most thirty days and never
starts in the future. Every list is bounded: `floating` by its limit (500 at most); `jobs`,
`outbox` and `burst` by the window — except what is still owed (`jobs` in `ready`/`leased`, `outbox` rows pending,
sending or ambiguous), which is listed at any age because a stuck row is the one to see; a
story's own lists and `cards` stop at 500 rows, `account` at 20.

## Watching

`--watch [--every 30]` re-reads and prints only what changed (`--json`: one envelope per read
with `changes` instead of `rows`; the first read prints every row as added). The first read is
the baseline: what was already failing is shown, not fatal. `floating --watch` exits 0 once
nothing floats for two reads and 6 when a floating story's retry job fails; `burst --watch`
exits 0 when nothing is mid-flight and 6 when a review card is raised; `jobs`/`outbox --watch`
exit 6 when a failed group appears or grows; the rest run until Ctrl-C, which exits 0. A
`story`, `cards` or `account` watch waits for a key that is not there yet. The post-deploy
read is `storydump burst --since <the deploy's time> --watch`.

## Acting on what you read

The write verbs go through the command port under your token — the same door a tap or a web
click uses, so admission, tenancy and audit apply unchanged — and to ONE workspace
(`--workspace <id or name>` is required). A story verb's idempotency key is deterministic
(`<command>:<story>`, the web's), so running it twice replays ("already done", exit 0) and
`--idempotency-key <k>` is the deliberate second execution; `resolve`'s key carries the review
episode, so a later review of the same story is new; `pause`, `resume` and `sync` mint a fresh
key per invocation, because their effects are idempotent and a later action must execute. A
refusal is an answer: the reason in the CLI's words, the fixing verb, exit 2.

| To … | Verb |
|---|---|
| skip, reject, or record a hand-posted story awaiting approval | `storydump skip|reject|posted <story> --workspace <ws>` |
| approve a story for the Instagram API (`manual_mode` when API posting is off) | `storydump approve <story> --workspace <ws>` |
| cancel a story (a waiting one is refunded; one mid-flight stops at its next step) | `storydump cancel <story> --workspace <ws>` |
| resolve a story parked for review | `storydump resolve <story> retry|posted|cancel [--not-posted] --workspace <ws>` |
| pause or resume the workspace's posting | `storydump pause --workspace <ws>` / `storydump resume --workspace <ws>` |
| queue a sync of a connected folder | `storydump sync <source_id> --workspace <ws>` |
| the deployment: the API's health, the latest deploys, the bot's webhook, this laptop | `storydump health` · `storydump deploys [--watch]` · `storydump webhook status` · `storydump doctor` |

## What the verbs never read

`rate_counters` (no workspace column) and the system jobs (`workspace_id IS NULL`): both sit
outside row-level security and are not a workspace's business. Every view carries its own
`workspace_id` predicate and is proven twice — as the ingress role under the policies, and as a
role that bypasses them (production's posture today) — so the rows are confined by the query,
not by the policy alone.

## The escape hatch

```bash
railway run --service worker --environment production -- \
  sh -c 'psql "$TARGET_DATABASE_URL" -At -F " | " -f /dev/stdin' < probe.sql \
  | sed -E "s#postgres(ql)?://[^ ]+#postgres://<redacted>#g"
```

Read-only. The connection string is never printed.
