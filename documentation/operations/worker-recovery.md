# Worker Recovery Runbook

**When to use:** the scheduler has been failing posts for hours or days, and `last_post_sent_at` in `chat_settings` is far in the past. The worker logs show `[catchup] Behind by N slot(s)` on every tick, never making progress.

This runbook walks through restarting the worker cleanly and confirming posts resume.

## Symptoms

- Telegram `/status` reports no recent posts.
- `railway logs --service worker` shows repeated lines like:
  ```
  [catchup] Behind by 134 slot(s) (last_sent=2026-05-13T02:33:01...)
  ```
- `CRITICAL ... N consecutive Telegram send failures` repeating with N growing.
- Setup wizard / dashboard appear healthy (storydump API service is up); only **posting** is dead.

## Background

The scheduler's catchup logic is intentional: when the worker comes back online after a brief outage it advances through missed slots to keep cadence. The pathology this runbook addresses is when posts have been **failing** (not just missing) for so long that the catchup runs every tick without making progress. Each tick:

1. Reads `last_post_sent_at` from `chat_settings` — sees it's far in the past
2. Computes how many slots are due (134, 200, etc.)
3. Tries to advance one slot — calls `select_and_send` → fails
4. `last_post_sent_at` never advances
5. Loop repeats next tick

The catchup cursor only moves on a **successful** send. If sends have been failing for systemic reasons (token revoked, session-detach race, etc.), the cursor stays pinned at the last good post and the catchup runs forever.

A fresh process resets the cursor logic — the catchup runs **once** at the new start (no longer "behind by 134 slots" because the new code path resets `last_post_sent_at` on first tick of a clean boot) and then the scheduler operates normally.

## Pre-flight check

Before restarting, confirm the **underlying cause** of the failures is fixed. A restart that lands on the same bug just re-enters the death spiral.

Common underlying causes:

| Symptom in logs | Underlying issue |
|---|---|
| `Instance <ChatSettings> is not bound to a Session` | [#388](https://github.com/chrisrogers37/storydump/issues/388) — session-detach race |
| `telegram.error.Unauthorized: 401` | Bot token revoked or rotated |
| `Conflict: terminated by other getUpdates request` on every retry | [#392](https://github.com/chrisrogers37/storydump/issues/392) — graceful shutdown bug |
| `psycopg2.errors.UndefinedColumn` | Migration not applied — see `documentation/operations/troubleshooting.md` |

If you don't know which one is firing, skim the most recent worker logs first:

```bash
railway logs --service worker | grep -E "ERROR|CRITICAL|Conflict|Unauthorized" | tail -20
```

## Recovery procedure

### 1. Confirm the fix PR has merged and Railway picked it up

```bash
gh pr view <fix-pr-number> --json state,mergedAt
git fetch origin main
git log --oneline origin/main -3
```

Confirm the merge commit is on `main`. Then check Railway started a deploy:

```bash
railway status --json | python3 -c "
import json, sys
data = json.load(sys.stdin)
for e in data['environments']['edges'][0]['node']['serviceInstances']['edges']:
    n = e['node']
    dep = n.get('latestDeployment') or {}
    print(f\"{n['serviceName']}: status={dep.get('status', '?')} commit={(dep.get('meta', {}) or {}).get('commitHash', '?')[:8]}\")
"
```

**If the worker shows `FAILED` while storydump shows `SUCCESS`, you have the [#392](https://github.com/chrisrogers37/storydump/issues/392) deploy-overlap issue.** The new code is live on the API but not the worker. Resolve #392 first or follow its workaround.

### 2. Trigger a clean redeploy

```bash
railway redeploy --service worker --yes
```

Wait ~2-3 minutes. Watch for the new commit hash:

```bash
railway status --json | python3 -c "..."  # same one-liner as above
```

### 3. Confirm fresh boot

```bash
railway logs --service worker | grep -E "Application startup|Starting Telegram bot polling|Storydump Started" | tail -5
```

You should see startup banners and the Telegram lifecycle "Storydump Started" notification within ~30s of the new process landing.

### 4. Verify the death spiral is broken

Watch the catchup logs:

```bash
railway logs --service worker | grep -E "catchup|select_and_send|posted" | tail -20
```

Healthy signs:
- The `[catchup] Behind by N` line appears **once or twice** then stops.
- `last_post_sent_at` advances to a recent timestamp (within the last hour).
- `SchedulerService.select_and_send` completes with `posted: true` in the result summary.

Unhealthy signs:
- Same `[catchup] Behind by` line every minute with `N` not decreasing — the underlying bug isn't fixed; **stop and re-investigate.**
- New `Instance not bound to a Session` or other session errors — the fix didn't ship correctly; check the deployed commit hash.

### 5. End-to-end confirmation

Wait for the next scheduled posting slot (default: every ~30 min) and confirm a post lands in the target Telegram channel.

For an immediate test, use the `/next` command in the Telegram bot — forces a slot to fire now without waiting.

## Risk assessment

**Low.** The redeploy is the same operation Railway runs on every push to `main`. The worker is making zero successful posts anyway, so the cost of restarting it is negligible.

The only risk is the underlying bug recurring after the restart — addressed by the pre-flight check above.

## Related

- [#388](https://github.com/chrisrogers37/storydump/issues/388) — primary session-detach bug
- [#392](https://github.com/chrisrogers37/storydump/issues/392) — graceful Telegram polling shutdown for clean redeploys
- `documentation/operations/troubleshooting.md` — general operational debugging
