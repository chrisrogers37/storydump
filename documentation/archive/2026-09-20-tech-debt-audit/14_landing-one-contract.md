---
title: "One owner per contract on the web tier, and the door to nowhere deleted"
type: plan
status: draft
owner: chris
created: 2026-09-20
tags: [plan, tech-debt, priority:high, landing]
links: []
---

# 14 — One owner per contract on the web tier, and the door to nowhere deleted

| | |
|---|---|
| **PR title** | One owner per contract on the web tier, and the door to nowhere deleted |
| **Risk** | Low — types, constants and deletions only; the one runtime door removed is unreachable (its single caller sits behind a hard-coded `editable={false}`) |
| **Effort** | M (≈4–6 h) |
| **Files modified** | `landing/src/lib/intents.ts`, `landing/src/lib/dashboard-payloads.ts`, `landing/src/lib/intent-states-contract.test.ts`, `landing/src/lib/telegram-link.ts`, `landing/src/lib/telegram-link.test.ts`, `landing/src/lib/types.ts`, `landing/src/lib/utils.ts`, `landing/src/lib/session.ts`, `landing/src/lib/commands.ts`, `landing/src/lib/destination.ts`, `landing/src/lib/drive.ts`, `landing/src/app/(dashboard)/dashboard/page.tsx`, `landing/src/app/(dashboard)/dashboard/media/calendar/page.tsx`, `landing/src/app/(dashboard)/dashboard/settings/page.tsx`, `landing/src/app/api/workspaces/route.ts`, `landing/src/components/dashboard/sidebar.tsx`, `landing/src/components/dashboard/analytics-cards.tsx`, `landing/src/components/dashboard/category-breakdown.tsx`, `landing/src/components/dashboard/posting-chart.tsx`, `landing/src/components/dashboard/recent-activity.tsx`, `landing/src/components/dashboard/media/pool-health.tsx`, `landing/src/components/dashboard/settings/accounts-tab.tsx`, `landing/src/components/dashboard/settings/api-tokens-tab.tsx`, `landing/src/components/dashboard/settings/integrations-tab.tsx`, `landing/src/lib/destination-badge-contract.test.ts` (comment only), `landing/next.config.ts`, `landing/package.json`, `landing/package-lock.json`, `CHANGELOG.md` — **deleted**: `landing/src/app/api/dashboard/[...path]/route.ts`, `landing/src/lib/dashboard-api.ts`, `landing/src/components/ui/progress.tsx`, `landing/src/components/ui/slider.tsx`; **new**: `landing/src/components/dashboard/tone.ts` |
| **Findings addressed** | TD-D1, TD-D3, TD-D4, TD-D8 |
| **Depends on** | — (can open on day one, in parallel with 01, 10, 11, 13) |
| **Blocks** | 15 (`15_landing-shared-shapes.md`) |

## Summary

Four findings that are all the same shape: a contract with two owners, or an
owner with nothing behind it.

`GET …/intents` is typed twice on this tier (`lib/intents.ts` and
`lib/dashboard-payloads.ts`), and the two copies have **already drifted** —
`IntentRow` is missing the two account columns the server has returned since
`_INTENT_COLUMNS` grew them, and disagrees on three nullabilities. The state
partition is likewise two objects: arrays in one file, comma strings in the
other, and the exhaustiveness contract test pins only the strings. This PR
makes `intents.ts` the single owner of the row type, the response envelope and
the state arrays, and derives the comma strings from the arrays, so the
contract test covers both by construction.

`/api/dashboard/[...path]` is a mounted, authenticated proxy whose allowlist
names 25 target paths of which 22 no longer exist, and whose only caller is a
button permanently disabled by a hard-coded `editable={false}`. It goes, with
its client (`lib/dashboard-api.ts`), its one caller, and the `editable` prop
that kept it on the screen.

Around those, the zero-importer leftovers (`jose`, two legacy types, two unused
`ui/` primitives, a test-only helper, a dead CSP override) and the shadow copies
of lib types in five components, a `Workspace` type declared twice, a badge-tone
union and class map written three times, and two UUID predicates that disagree
about what a UUID is.

Everything here is types, constants, deletions and imports. Nothing changes what
a running screen computes. The two user-visible consequences are named
explicitly in the steps: the **Analytics nav item** disappears from the sidebar
(step 12), and `/login` **stops carrying a CSP header** that today blocks the
site's own analytics script (step 9). Both are argued in place.

## Findings addressed

| ID | Where | One line |
|---|---|---|
| TD-D1 | `landing/src/lib/intents.ts:22-72` vs `landing/src/lib/dashboard-payloads.ts:50-70,161-215` | One endpoint typed twice, already drifted on two columns and three nullabilities; the state partition is arrays here and comma strings there. |
| TD-D3 | `landing/src/app/api/dashboard/[...path]/route.ts`, `landing/src/lib/dashboard-api.ts`, `landing/src/components/dashboard/settings/accounts-tab.tsx`, `landing/src/app/(dashboard)/dashboard/settings/page.tsx:293` | An authenticated proxy to 22 routes that do not exist, kept alive by one control that is hard-coded off. |
| TD-D4 | `landing/package.json:23`, `landing/src/lib/types.ts:32-50`, `landing/src/lib/dashboard-payloads.ts:465-507`, `landing/src/lib/utils.ts:8-16`, `landing/src/components/ui/{progress,slider}.tsx`, `landing/src/lib/telegram-link.ts:86-90`, `landing/next.config.ts:22-33`, `landing/src/components/dashboard/sidebar.tsx:22` | Zero-importer leftovers, plus a live CSP block for a widget that no longer exists. |
| TD-D8 | `analytics-cards.tsx:24-32`, `category-breakdown.tsx:28-34`, `media/pool-health.tsx:18-25`, `posting-chart.tsx:26-30`, `recent-activity.tsx:16-22`, `api/workspaces/route.ts:6-11`, `lib/destination.ts:61-64`, `lib/drive.ts:61-64`, `api-tokens-tab.tsx:91-94,112-116`, `accounts-tab.tsx:54-58`, `integrations-tab.tsx:556-562`, `lib/session.ts:242-247`, `lib/commands.ts:58-63` | Five shadow copies of lib types, `Workspace` declared twice, a badge tone union/class map written three times, two UUID predicates. |

Nothing in this doc is withdrawn. Every `path:line` in the table was re-read at
`0966771` and matched.

**Re-reading confirmed the D1 drift is real.** `src/services/target/workspaces.py:381-386`:

```python
_INTENT_COLUMNS = (
    "i.id, i.state, i.ig_account_id, i.media_item_id, i.schedule_slot_at,"
    " i.approval_mode, i.published_via, i.publish_step, i.cancel_requested,"
    " i.ig_permalink, i.entered_state_at, i.created_at,"
    " m.file_name, m.media_kind, m.thumbnail_url, m.caption, m.category,"
    " a.handle AS account_handle, a.display_name AS account_display_name"
)
```

`account_handle` and `account_display_name` are served; `IntentRow` does not
declare them. `Intent` does.

**Re-reading confirmed the D3 allowlist is dead.** `grep -n "@router\.\(get\|post\|patch\|delete\|put\)" src/api/routes/v1.py` returns 28 routes.
Of the proxy's 25 allowlisted top segments, exactly three name a live target
path — `accounts`, `category-mix`, `media` — and all three already have a
dedicated site route (`app/api/workspaces/[id]/{accounts,category-mix}/…`, and
media is read server-side through `workspaceFetch`). The other 22 (`analytics`,
`audit-log`, `history-detail`, `media-library`, `media-stats`, `queue-detail`,
`queue-preview`, `system-status`, `toggle-setting`, `update-setting`,
`update-string-setting`, `update-category-mix`, `switch-account`, `sync-media`,
`init`, `schedule`, `complete`, `media-folder`, `start-indexing`, `add-account`,
`remove-account`, `disconnect-gdrive`) are served by nothing.

## Dependencies

- **Depends on:** nothing. This is one of the five docs that can open on day one.
- **Blocks:** `15_landing-shared-shapes.md`. Doc 15 edits `accounts-tab.tsx`,
  `general-tab.tsx`, `api-tokens-tab.tsx` and `integrations-tab.tsx` heavily
  (notice banners, dead props, docblock moves); this PR removes the `editable`
  prop from `AccountsTab` and the tone maps from two of those files first, so
  doc 15 is not resolving conflicts against a deleted prop.
- **Parent commit:** `main` at `0966771`.

## Implementation Plan

### Steps

1. **Baseline** — from the repo root, prove the tree is green before touching it.

   ```bash
   npm --prefix landing run test
   npx --prefix landing tsc --noEmit
   npm --prefix landing run lint
   ```

   All three must pass. `git status` must be clean afterwards (two contract
   tests read the Python source; none writes).

---

#### TD-D1 — one owner for the intents contract

2. **`intents.ts` states that it is the owner** — `landing/src/lib/intents.ts:41-43`.
   No code change; one sentence added to the `INTENT_STATES` docblock so the
   next reader knows the comma strings are derived and not a second source.

   Before:

   ```ts
   /** `ck_intent_state`, the closed set — mirrors `workspaces.INTENT_STATES` (Python); this comment is the grep handle from either side. */
   export const INTENT_STATES = [...NON_TERMINAL_STATES, ...TERMINAL_STATES] as const;
   ```

   After:

   ```ts
   /**
    * `ck_intent_state`, the closed set — mirrors `workspaces.INTENT_STATES`
    * (Python); this comment is the grep handle from either side.
    *
    * THE ARRAYS ABOVE ARE THE ONLY SOURCE ON THIS TIER. `dashboard-payloads.ts`
    * used to declare a second partition as comma strings for the `?state=`
    * query; it now `join(",")`s these. There was never a reason for two, and
    * the two had already come apart: one of them had a row type missing
    * `account_handle`/`account_display_name`, which `_INTENT_COLUMNS` has
    * served since `06` §3.
    */
   export const INTENT_STATES = [...NON_TERMINAL_STATES, ...TERMINAL_STATES] as const;
   ```

   Importer grep — nothing changes, this is a comment:

   ```bash
   grep -rn "INTENT_STATES" landing/src
   ```

   Expected before and after: the same 12 hits (3 in `intents.ts`, 2 in
   `intents.test.ts`, 1 comment in `dashboard-payloads.ts`, 6 in
   `intent-states-contract.test.ts`).

3. **Delete the second row type and the second response envelope** —
   `landing/src/lib/dashboard-payloads.ts:49-70`.

   Before:

   ```ts
   /** A row of `GET …/intents?state=&limit=` — `_INTENT_COLUMNS`, joined to media. */
   export type IntentRow = {
     id: string;
     state: string;
     ig_account_id: string | null;
     media_item_id: string;
     schedule_slot_at: string | null;
     approval_mode: string | null;
     published_via: string | null;
     publish_step: string | null;
     cancel_requested: boolean;
     ig_permalink: string | null;
     entered_state_at: string;
     created_at: string;
     file_name: string;
     media_kind: string;
     thumbnail_url: string | null;
     caption: string | null;
     category: string | null;
   };

   export type IntentsResponse = { intents: IntentRow[]; limit: number };
   ```

   After (the whole block above is replaced by a re-export, so a reader who
   lands here is sent to the owner rather than finding nothing):

   ```ts
   /**
    * The intent row and its envelope live in `intents.ts` — ONE contract.
    *
    * This file used to declare a second `IntentRow`/`IntentsResponse` pair for
    * the three screens that read `?state=`. Structural typing kept both
    * compiling while they drifted: this copy was missing `account_handle` and
    * `account_display_name` (served by `_INTENT_COLUMNS`), and typed
    * `ig_account_id`, `schedule_slot_at` and `approval_mode` as nullable where
    * the server never sends null. Re-exported rather than deleted outright so
    * that the three screens keep one import site for everything they read from
    * the intents endpoint.
    */
   export type { Intent, IntentState, IntentsResponse } from "./intents";
   ```

   Importer grep:

   ```bash
   grep -rn "IntentRow" landing/src
   ```

   Expected **before**: 4 hits — `media/calendar/page.tsx:9`,
   `media/calendar/page.tsx:19`, `dashboard-payloads.ts:50`,
   `dashboard-payloads.ts:70`.
   Expected **after**: 0 hits.

   ```bash
   grep -rn "IntentsResponse" landing/src
   ```

   Expected **before**: 9 hits across `dashboard/page.tsx` (×2),
   `queue/page.tsx` (×2), `media/calendar/page.tsx` (×4),
   `dashboard-payloads.ts:70`, `intents.ts:72`.
   Expected **after**: 9 hits, with `dashboard-payloads.ts:70` now a re-export
   line rather than a declaration and `intents.ts` the only `export type
   IntentsResponse = …`. Confirm with:

   ```bash
   grep -rn "export type IntentsResponse" landing/src
   ```

   Expected **before**: 2 hits. Expected **after**: 1 hit (`intents.ts`).

   No test to add here; step 6 pins the derivation.

4. **Derive the comma strings from the arrays** —
   `landing/src/lib/dashboard-payloads.ts:161,185-201`.

   Add the import at the top of the file (the file currently has no top-level
   imports; put it on line 1, above the module docblock's closing, i.e. as the
   first statement after the docblock):

   ```ts
   import { NON_TERMINAL_STATES, TERMINAL_STATES as TERMINAL_STATE_LIST } from "./intents";
   ```

   There is no cycle: `intents.ts` imports only `./refusal-copy`, and
   `dashboard-payloads.ts` reaches `./types` through an inline `import("./types")`
   type position. Verify with:

   ```bash
   grep -n "^import" landing/src/lib/intents.ts landing/src/lib/refusal-copy.ts
   ```

   Expected: `intents.ts` imports `./refusal-copy` only; `refusal-copy.ts`
   imports nothing from `dashboard-payloads`.

   Before (`:185-201`):

   ```ts
   export const QUEUE_STATES =
     "scheduled,prompt_pending,awaiting_approval,approved," +
     "publishing,publishing_ambiguous,review_required";
   ```

   ```ts
   export const TERMINAL_STATES =
     "posted,skipped,rejected,expired,failed,cancelled";
   ```

   After — **keep both docblocks exactly as they are** (they are the reasoning
   for the partition and #1044's call, and none of it stops being true); change
   only the right-hand side, and append one sentence to each:

   ```ts
   export const QUEUE_STATES = NON_TERMINAL_STATES.join(",");
   ```

   ```ts
   export const TERMINAL_STATES = TERMINAL_STATE_LIST.join(",");
   ```

   Append to the `QUEUE_STATES` docblock, after the existing final paragraph:

   ```
    * DERIVED, NOT RE-TYPED (#NNNN). The members are `NON_TERMINAL_STATES` in
    * `intents.ts`; this is the `?state=` spelling of them. A state added there
    * now lands here without an edit, which is the failure mode the paragraph
    * above describes, closed at the source rather than watched for.
   ```

   Append to the `TERMINAL_STATES` docblock, same place:

   ```
    * DERIVED from `intents.TERMINAL_STATES`, which mirrors
    * `command_executors.TERMINAL_STATES` (Python). The superset relationship
    * with `HISTORY_STATES` below is unchanged and still this tier's call.
   ```

   The rendered values must be byte-identical to today's literals. Both
   orderings match: `NON_TERMINAL_STATES` is `scheduled, prompt_pending,
   awaiting_approval, approved, publishing, publishing_ambiguous,
   review_required`; `TERMINAL_STATES` is `posted, skipped, rejected, expired,
   failed, cancelled`. Step 6 adds the pin that says so.

   Importer grep:

   ```bash
   grep -rn "QUEUE_STATES\|TERMINAL_STATES" landing/src
   ```

   Expected **before**: 22 hits. Expected **after**: the same 22 sites, plus the
   new import line in `dashboard-payloads.ts`; no call site changes, because
   both exports keep their name, their type (`string`) and their value.

5. **Point the three screens at the one owner.**

   5a. `landing/src/app/(dashboard)/dashboard/media/calendar/page.tsx:4-13,19`.

   Before:

   ```ts
   import {
     HISTORY_STATES,
     QUEUE_STATES,
     REVIEW_REQUIRED_STATE,
     SCHEDULED_STATES,
     type IntentRow,
     type IntentsResponse,
     type StatsResponse,
     type WorkspaceConfig,
   } from "@/lib/dashboard-payloads";
   ```

   After:

   ```ts
   import {
     HISTORY_STATES,
     QUEUE_STATES,
     REVIEW_REQUIRED_STATE,
     SCHEDULED_STATES,
     type StatsResponse,
     type WorkspaceConfig,
   } from "@/lib/dashboard-payloads";
   import type { Intent, IntentsResponse } from "@/lib/intents";
   ```

   Before (`:19`):

   ```ts
   const laneItem = (i: IntentRow) => ({
   ```

   After:

   ```ts
   const laneItem = (i: Intent) => ({
   ```

   `ContentCalendar`'s `HistoryItem`/`QueueItem` declare `status: string`
   (`content-calendar.tsx:12-30`); `Intent["state"]` is the `IntentState`
   union, which is assignable to `string`. No change there.

   5b. Same file, `:83-92` — the two `as string` casts become redundant under
   the stricter type. **Leave them.** `Intent.schedule_slot_at` is `string`, so
   `i.schedule_slot_at as string` is a no-op assertion that `tsc` accepts and
   eslint does not flag (`@typescript-eslint/no-unnecessary-type-assertion`
   requires type-aware linting, which `eslint.config.mjs` does not enable —
   confirm with `npm --prefix landing run lint` in step 15). Removing them is a
   behavioural no-op but touches the two `.filter(…)` chains that are the only
   thing keeping a slot-less intent off the calendar; this PR does not go near
   them. Note as a follow-up.

   > If `tsc --noEmit` *does* complain about either cast, delete the cast (keep
   > the `.filter`) and nothing else.

   5c. `landing/src/app/(dashboard)/dashboard/page.tsx:4-10`.

   Before:

   ```ts
   import {
     HISTORY_STATES,
     deriveCategories,
     deriveSummary,
     type IntentsResponse,
     type StatsResponse,
   } from "@/lib/dashboard-payloads";
   ```

   After:

   ```ts
   import {
     HISTORY_STATES,
     deriveCategories,
     deriveSummary,
     type StatsResponse,
   } from "@/lib/dashboard-payloads";
   import type { IntentsResponse } from "@/lib/intents";
   ```

   5d. `landing/src/app/(dashboard)/dashboard/queue/page.tsx:5` — **no change**.
   It already imports `NON_TERMINAL_STATES` and `IntentsResponse` from
   `@/lib/intents`, and `:40` already spells the query as
   `NON_TERMINAL_STATES.join(",")`. That call site is the pattern this step
   generalises; leave it exactly as it is.

   Importer grep for the whole step:

   ```bash
   grep -rn 'from "@/lib/intents"' landing/src
   ```

   Expected **before**: 2 hits (`queue/page.tsx:5`, `queue-list.tsx`).
   Expected **after**: 4 hits (+ `dashboard/page.tsx`, `media/calendar/page.tsx`).

6. **Point the exhaustiveness contract test at the arrays** —
   `landing/src/lib/intent-states-contract.test.ts:5-11,72-90`.

   The test currently imports only the comma strings. After step 4 those are
   derived, so the test would still pass while proving nothing about the arrays
   that feed them. Add the arrays to the imports and add one case that pins the
   derivation.

   Before (`:5-11`):

   ```ts
   import {
     HISTORY_STATES,
     QUEUE_STATES,
     REVIEW_REQUIRED_STATE,
     SCHEDULED_STATES,
     TERMINAL_STATES,
   } from "./dashboard-payloads";
   ```

   After:

   ```ts
   import {
     HISTORY_STATES,
     QUEUE_STATES,
     REVIEW_REQUIRED_STATE,
     SCHEDULED_STATES,
     TERMINAL_STATES,
   } from "./dashboard-payloads";
   import {
     INTENT_STATES,
     NON_TERMINAL_STATES,
     TERMINAL_STATES as TERMINAL_STATE_LIST,
   } from "./intents";
   ```

   New case, added inside the existing
   `describe("the intent-state partition agrees with the API", …)` block,
   immediately after the `"accounts for EVERY state"` case:

   ```ts
   it("keeps the query spellings derived from the arrays, not re-typed", () => {
     // The `?state=` strings USED to be a second, hand-written partition in
     // `dashboard-payloads.ts`, and this file pinned only those. The arrays in
     // `intents.ts` were pinned only against a hand-typed literal in
     // `intents.test.ts` — a copy agreeing with itself, which is the exact
     // anti-pattern the docblock at the top of this file names. Now one is
     // derived from the other; this case is what makes that structural rather
     // than a convention.
     expect(split(QUEUE_STATES)).toEqual([...NON_TERMINAL_STATES]);
     expect(split(TERMINAL_STATES)).toEqual([...TERMINAL_STATE_LIST]);
     expect(new Set([...split(QUEUE_STATES), ...split(TERMINAL_STATES)])).toEqual(
       new Set(INTENT_STATES),
     );
   });
   ```

   Nothing else in the file changes: the `apiIntentStates()` reader, the
   exhaustiveness case, the one-half case, the `review_required` case and the
   two subset cases all keep working on the derived strings.

   Run just this file:

   ```bash
   npx --prefix landing vitest run src/lib/intent-states-contract.test.ts
   ```

   Expected: 6 passing (5 existing + 1 new).

7. **Leave `intents.test.ts:104-114` as it is.** It asserts
   `NON_TERMINAL_STATES` against a hand-typed literal. That is now the only
   hand-typed copy on the tier, and after step 6 the contract test reads the
   API's own tuple and checks both halves against it — so the literal is a
   deliberate second opinion on the *order* rather than a second source of
   truth. Add one sentence above it:

   ```ts
   it("knows the non-terminal states the page lists", () => {
     // A hand-typed second opinion on the ORDER, kept deliberately: the
     // membership question is answered against the API's own tuple by
     // `intent-states-contract.test.ts`, which also pins the `?state=` query
     // spellings as `join(",")` of these. This case fails if someone reorders
     // the array, which the query strings would otherwise absorb silently.
     expect(NON_TERMINAL_STATES).toEqual([
   ```

---

#### TD-D3 — delete the door to nowhere

8. **Delete the proxy, its client and its one call site.** Do the four
   deletions in this order so that `tsc` points at the next one.

   8a. Delete the route directory:

   ```bash
   git rm -r "landing/src/app/api/dashboard"
   ```

   (132 lines; `GET`/`POST`/`PUT`/`PATCH`/`DELETE` all bound to `proxyRequest`.)

   8b. Delete the client:

   ```bash
   git rm landing/src/lib/dashboard-api.ts
   ```

   (27 lines: `postApi`, `getApi`. `getApi` has zero importers today.)

   8c. `landing/src/components/dashboard/settings/accounts-tab.tsx` — six edits.

   Delete the import at `:18`:

   ```ts
   import { postApi } from "@/lib/dashboard-api";
   ```

   Delete `DISABLED_REASON` and its docblock at `:33-42`:

   ```ts
   /**
    * Connect is real and ungated: the header's *Connect Instagram* ADDS a
    * destination through the Instagram Login grant (owner ruling 2026-09-04), and
    * each row's Connect/Reconnect acts on the account it names, and Remove is the
    * port's `disable_account`. `switch-account` is a real control whose route is
    * not wired yet (#1063 / epic P6) — DISABLED WITH A REASON, not removed, so
    * the screen does not lose a capability the user is about to get.
    */
   const DISABLED_REASON =
     "Not wired up yet — changing accounts is not available on this API version.";
   ```

   Replace with a docblock that states what the screen does now — the first
   three clauses were true and stay, the fourth was the dead one:

   ```ts
   /**
    * Connect is real and ungated: the header's *Connect Instagram* ADDS a
    * destination through the Instagram Login grant (owner ruling 2026-09-04),
    * each row's Connect/Reconnect acts on the account it names, and Remove is
    * the port's `disable_account`.
    *
    * SWITCHING IS GONE FROM THIS SCREEN (#NNNN). It was a "Make Active" button
    * disabled with a reason, on the argument that the screen should not lose a
    * capability it was about to get. The capability did not arrive: the button
    * POSTed to `/api/dashboard/switch-account`, a BFF proxy onto a target path
    * that does not exist and has no entry in `COMMAND_SPECS`. A control that
    * cannot be pressed is not a promise, it is furniture, and the proxy behind
    * it was an authenticated door onto 22 routes the API stopped serving. When
    * P6 lands, switching comes back as a `switch_account` row in
    * `lib/commands.ts` and a button that calls `submitCommand`, like every
    * other write on this tab.
    */
   ```

   Delete `switchAccount` at `:103-114`:

   ```ts
   async function switchAccount(accountId: string) {
     setError(null);
     setLoadingAction(`switch-${accountId}`);
     try {
       await postApi("switch-account", { account_id: accountId });
       router.refresh();
     } catch (e) {
       setError(e instanceof Error ? e.message : "Failed to switch account");
     } finally {
       setLoadingAction(null);
     }
   }
   ```

   Delete the button at `:223-235`:

   ```tsx
   {!isActive && (
       <Button
         variant="outline"
         size="sm"
         onClick={() => switchAccount(account.id)}
         disabled={!editable || loadingAction === `switch-${account.id}`}
         title={editable ? undefined : DISABLED_REASON}
       >
         {loadingAction === `switch-${account.id}`
           ? "Activating..."
           : "Make Active"}
       </Button>
     )}
   ```

   Delete the footnote at `:279-284`:

   ```tsx
   {!editable && (
     <p className="mt-4 text-xs text-muted-foreground">
       Switching accounts is not wired up yet — the control is shown
       disabled rather than hidden, because it is coming back.
     </p>
   )}
   ```

   Delete the `editable` prop — `:60-63` and `:67`:

   ```ts
   interface AccountsTabProps {
     /** False while the write routes do not exist (#1063). */
     editable: boolean;
     accounts: Destination[];
     workspaceId: string;
   }

   export function AccountsTab({ accounts, editable, workspaceId }: AccountsTabProps) {
   ```

   After:

   ```ts
   interface AccountsTabProps {
     accounts: Destination[];
     workspaceId: string;
   }

   export function AccountsTab({ accounts, workspaceId }: AccountsTabProps) {
   ```

   8d. **`isActive` becomes unused — this is the one non-obvious consequence.**
   `:223` was the last reader of `isActive`. After deleting the button, `:176`
   and the `destinationIsActive` import at `:25` are dead and
   `@typescript-eslint/no-unused-vars` fails the build. Delete both.

   Before (`:25` inside the `@/lib/destination` import list):

   ```ts
     destinationIsActive,
   ```

   Before (`:176`):

   ```ts
                   const isActive = destinationIsActive(account.state);
   ```

   Both lines go. `stateBadge` at `:177` stays; it is what the badge renders.

   8e. **`destination-badge-contract.test.ts` keeps passing, but its comment
   goes stale.** Its third case asserts
   `not.toMatch(/(?<![!\w])isActive\s*&&/)` — still satisfied, because there is
   now no `isActive` at all. The docblock on that case says the lookbehind is
   load-bearing *because* `{!isActive && <Button>Make Active</Button>}` is a
   legitimate remaining caller. That caller is gone. Update the comment only —
   **do not touch the assertion** (the regex is the tripwire on the original
   regression and must survive this PR intact):

   Before (`landing/src/lib/destination-badge-contract.test.ts:62-71`):

   ```ts
       // The regression, verbatim: `{isActive && (<Badge …>Active</Badge>)}`.
       //
       // The lookbehind is load-bearing and the first version of this test was
       // wrong without it. `destinationIsActive` still has a legitimate caller on
       // this screen — `{!isActive && <Button>Make Active</Button>}`, which is a
       // genuine boolean question — and `isActive` is a substring of `!isActive`,
       // so the naive pattern failed on correct code. The guard being forbidden
       // is the POSITIVE one.
   ```

   After:

   ```ts
       // The regression, verbatim: `{isActive && (<Badge …>Active</Badge>)}`.
       //
       // The lookbehind is load-bearing and the first version of this test was
       // wrong without it. `isActive` is a substring of `!isActive`, so the
       // naive pattern failed on correct code: the guard being forbidden is the
       // POSITIVE one. The negative form had a legitimate caller on this screen
       // — `{!isActive && <Button>Make Active</Button>}` — until #NNNN deleted
       // the switch-account control and its dead proxy. The lookbehind stays
       // anyway: it is what makes this case say "no positive guard" rather than
       // "no mention of isActive", and the next `!isActive` on this screen must
       // not be the thing that turns the badge back into a boolean.
   ```

   8f. `landing/src/app/(dashboard)/dashboard/settings/page.tsx:283-296` — drop
   the prop and rewrite the comment that explained it.

   Before:

   ```tsx
   {/*
     NOT flipped with General: `editable` now gates only `switch-account`,
     which has no target-tier home yet (epic P6) — Connect and Remove are
     real and ungated inside the tab (Remove = `disable_account`,
     owner decision 2026-09-04).
   */}
   <TabsContent value="accounts">
     <AccountsTab
       accounts={accounts}
       editable={false}
       workspaceId={workspaceId}
     />
   </TabsContent>
   ```

   After:

   ```tsx
   {/*
     No `editable` here any more (#NNNN): the one control it gated was
     "Make Active", which POSTed through a BFF proxy onto a target path that
     does not exist. Both are deleted. Connect and Remove are real and
     ungated inside the tab (Remove = `disable_account`, owner decision
     2026-09-04), so the tab has nothing left that is pending.
   */}
   <TabsContent value="accounts">
     <AccountsTab accounts={accounts} workspaceId={workspaceId} />
   </TabsContent>
   ```

   Also update the `editable`-is-per-tab essay at `:35-64` — it names Accounts
   as one of the "NOT editable" tabs. Change the sentence that says so to state
   that Accounts no longer takes the flag, and why. Leave the rest of the essay
   (it still governs Integrations and the General tab).

   Importer greps for the whole step:

   ```bash
   grep -rn "dashboard-api\|postApi\|getApi\|switchAccount\|DISABLED_REASON" landing/src
   ```

   Expected **before**: 10 hits — `accounts-tab.tsx:18,41,103,107,227,229`,
   `general-tab.tsx:33` (prose), `commands.ts:10` (prose),
   `dashboard-api.ts:6,19`.
   Expected **after**: 2 hits, both prose in docblocks that are describing the
   past correctly (`general-tab.tsx:33` "They used to be `postApi(…)`",
   `commands.ts:10` "speaking a second, dead dialect through `postApi`"). Leave
   both — they are history, not references.

   ```bash
   grep -rn "api/dashboard" landing/src
   ```

   Expected **before**: 3 hits. Expected **after**: 0 hits.

   ```bash
   grep -rn "editable" landing/src/components/dashboard/settings/accounts-tab.tsx
   ```

   Expected **before**: 5 hits. Expected **after**: 0 hits.

   ```bash
   grep -rn "destinationIsActive\|isActive" landing/src
   ```

   Expected **before**: 3 hits in `accounts-tab.tsx` (`:25,:176,:223`), plus
   `destination.ts`'s own export and the contract test's regex/comment.
   Expected **after**: 0 hits in `accounts-tab.tsx`; `destination.ts` keeps
   `destinationIsActive` (it has a unit test in `destination.test.ts`) and the
   contract test keeps its regex.

   > `destinationIsActive` is now exported with no importer outside its test.
   > **Keep it.** It is a one-line pure predicate on a closed vocabulary with a
   > pinned unit test, and P6 brings back the caller. This is a judgment the
   > plan is making once, here, so the builder does not have to: do not delete
   > it, and do not add it to the D4 sweep below.

   Test to add/update: none. No test imports the proxy or `dashboard-api.ts`.
   `settings-loading-contract.test.tsx` does not reference `editable` — confirm
   with `grep -rn "editable" landing/src/**/*.test.*` (expected: 0 hits).

---

#### TD-D4 — the zero-importer sweep

9. **Delete the `/login` CSP override** — `landing/next.config.ts:22-33`.

   Before:

   ```ts
         // Telegram Login Widget needs 'unsafe-inline' for its injected script
         // and 'unsafe-eval' because telegram-widget.js eval()s the data-onauth handler
         {
           source: "/login",
           headers: [
             {
               key: "Content-Security-Policy",
               value:
                 "script-src 'self' 'unsafe-inline' 'unsafe-eval' https://telegram.org; frame-src https://oauth.telegram.org;",
             },
           ],
         },
   ```

   After: the whole block is removed, leaving `headers()` returning only the
   global `/:path*` entry (`X-Frame-Options: DENY`, `X-Content-Type-Options: nosniff`).

   **Delete rather than tighten, and this is load-bearing.** Two facts decide it:

   - `app/layout.tsx:80-87` renders the Plausible `<Script>` on **every** page,
     including `/login`, from `https://plausible.io/js/script.js`. The current
     `/login` policy lists `'self' 'unsafe-inline' 'unsafe-eval' https://telegram.org`
     and **not** `plausible.io` — so today, on `/login` only, that script is
     blocked by CSP. Removing the header is what makes `/login` behave like
     every other page on the site.
   - Tightening to `script-src 'self'` would be worse than either: the App
     Router streams hydration payloads as inline `<script>` tags, which
     `'self'` alone forbids. `/login` would stop hydrating. Do not do this.

   `login/page.tsx:13-16` states the widget is "gone rather than hidden" and
   that no configuration brings it back, so nothing needs the grant.

   Importer grep:

   ```bash
   grep -rn "telegram.org\|unsafe-eval\|unsafe-inline\|Content-Security-Policy" landing/next.config.ts landing/src
   ```

   Expected **before**: 2 hits in `next.config.ts` (the comment and the value).
   Expected **after**: 0 hits.

   Manual smoke (step 17): load `/login`, confirm the Google button renders and
   the page hydrates, and confirm DevTools › Network shows no CSP violation and
   (with `NEXT_PUBLIC_PLAUSIBLE_DOMAIN` set) that `script.js` loads.

10. **Drop `jose`** — `landing/package.json:23`.

    ```bash
    grep -rn "jose" landing/src
    ```

    Expected **before**: 0 hits. Expected **after**: 0 hits. (`session.ts:6-8`
    documents the HS256 session it replaced.)

    ```bash
    npm --prefix landing uninstall jose
    ```

    This is the one `npm` write this PR makes; commit both `package.json` and
    `package-lock.json`. Nothing else in the PR needs `npm install`.

11. **Delete the zero-importer exports and files.** Run each grep first; each
    must return only the definition site (and, for `telegramLinkedFrom`, its
    test) before the deletion.

    | Target | Grep | Expected before | Expected after |
    |---|---|---|---|
    | `landing/src/lib/types.ts:32-50` (`InstagramAccount`, `Instance`) | `grep -rn "InstagramAccount\|\bInstance\b" landing/src` | 4 hits: `dashboard-payloads.ts:461` (prose), `types.ts:5` (prose), `types.ts:33`, `types.ts:41` | 2 hits, both prose |
    | `landing/src/lib/dashboard-payloads.ts:465-507` (the "STILL LEGACY" block, `SetupState`, `InitResponse`) | `grep -rn "SetupState\|InitResponse" landing/src` | 2 hits, both in `dashboard-payloads.ts` | 0 hits |
    | `landing/src/lib/utils.ts:8-16` (`formatLastPost`) | `grep -rn "formatLastPost" landing/src` | 1 hit (the definition) | 0 hits |
    | `landing/src/components/ui/progress.tsx` | `grep -rn "ui/progress\|<Progress" landing/src` | 2 hits, both inside `progress.tsx` | 0 hits |
    | `landing/src/components/ui/slider.tsx` | `grep -rn "ui/slider\|<Slider" landing/src` | 4 hits, all inside `slider.tsx` | 0 hits |
    | `landing/src/lib/telegram-link.ts:86-90` (`telegramLinkedFrom`) + `telegram-link.test.ts:15,111-119` | `grep -rn "telegramLinkedFrom" landing/src` | 7 hits: the definition, the test import, 5 in the test block | 0 hits |

    For `types.ts`, delete both interfaces **and** fix the now-dangling
    cross-reference at `types.ts:5`:

    Before:

    ```ts
    * Separate from `InstagramAccount`, which is the LEGACY payload shape and now
    ```

    After:

    ```ts
    * The LEGACY payload shape it replaced (`InstagramAccount`: `display_name`,
    * `instagram_username`, `is_active`) is deleted (#NNNN) — it had no
    * consumers and the target serves `handle`/`state`.
    ```

    (Read `types.ts:1-10` and re-flow the sentence; the surrounding docblock
    continues after this line.)

    For `dashboard-payloads.ts`, the deleted block at `:465-484` is also one of
    TD-D12's stale claims ("the screen stays on `init`", contradicted by
    `deriveSettings` at `:343-458`). It goes with the types it introduced — so
    doc 15's D12 pass has one fewer item. Say so in doc 15's Origin note.

    For `telegram-link.ts`, delete the export and its docblock; the same
    expression is inlined at `session.ts:180`, which is the live reader.
    In `telegram-link.test.ts`, delete the import on `:15` and the whole
    `describe("telegramLinkedFrom", …)` block at `:111-119`.

    Test to update: `landing/src/lib/telegram-link.test.ts`. Run it alone after:

    ```bash
    npx --prefix landing vitest run src/lib/telegram-link.test.ts
    ```

    Expected: green, with the `telegramLinkedFrom` describe gone from the report.

12. **Remove the Analytics nav item; keep the page.**
    `landing/src/components/dashboard/sidebar.tsx:5-12,16-23`.

    Before:

    ```ts
    import {
      BarChart3,
      CalendarDays,
      ImageIcon,
      LayoutDashboard,
      ListChecks,
      Settings,
    } from "lucide-react";
    ```

    ```ts
    const navItems = [
      { href: "/dashboard", label: "Overview", icon: LayoutDashboard },
      { href: "/dashboard/queue", label: "Queue", icon: ListChecks },
      { href: "/dashboard/media", label: "Media Library", icon: ImageIcon },
      { href: "/dashboard/media/calendar", label: "Calendar", icon: CalendarDays },
      { href: "/dashboard/settings", label: "Settings", icon: Settings },
      { href: "/dashboard/analytics", label: "Analytics", icon: BarChart3 },
    ];
    ```

    After:

    ```ts
    import {
      CalendarDays,
      ImageIcon,
      LayoutDashboard,
      ListChecks,
      Settings,
    } from "lucide-react";
    ```

    ```ts
    const navItems = [
      { href: "/dashboard", label: "Overview", icon: LayoutDashboard },
      { href: "/dashboard/queue", label: "Queue", icon: ListChecks },
      { href: "/dashboard/media", label: "Media Library", icon: ImageIcon },
      { href: "/dashboard/media/calendar", label: "Calendar", icon: CalendarDays },
      { href: "/dashboard/settings", label: "Settings", icon: Settings },
    ];
    ```

    Extend the existing docblock at `:25-37` (the "a nav item is a promise that
    a destination exists" paragraph) with the second application of its own rule:

    ```
     * THE SAME RULE, APPLIED AGAIN (#NNNN). `/dashboard/analytics` was in this
     * list and its destination renders one card reading "Coming Soon … planned
     * for Phase 3". A nav item is a promise that a destination exists; a
     * destination that exists only to say it does not is the same broken
     * promise with a softer landing. The entry is gone.
     *
     * THE PAGE IS NOT. Deleting it would turn a URL that answers 200 today into
     * a 404 for anyone holding the link, which is a behaviour change this
     * cleanup does not get to make. It comes back to this list with the screen
     * it names, or it is deleted under its own ruling.
    ```

    **This is the decision, stated once so the builder does not make it:**
    remove the nav item, keep `app/(dashboard)/dashboard/analytics/page.tsx`.
    Reasons: (a) the page is reachable by URL and still answers, so no one
    following a bookmark gets a 404; (b) the sidebar is where the promise is
    made, so that is where withdrawing it belongs; (c) deleting the page is a
    product decision about whether Phase 3 analytics is still planned, which is
    not this PR's to take. The alternative — delete both — is listed in
    "Related" as a follow-up ruling.

    This is a **user-visible change**: the Analytics link disappears from the
    dashboard sidebar. It is the only pixel this PR moves. Named here so a
    reviewer can veto it without reading the diff.

    Importer grep:

    ```bash
    grep -rn "BarChart3\|dashboard/analytics" landing/src
    ```

    Expected **before**: 3 hits — `sidebar.tsx:6`, `sidebar.tsx:22`, and the
    page's own directory path (no source hit).
    Expected **after**: 0 hits in `landing/src/components` and
    `landing/src/app/(dashboard)/dashboard/*.tsx`; the page file itself remains
    on disk.

    Test: none exists for the sidebar. Do not add one — `vitest.config.ts` sets
    `environment: "node"` and the nav list is not a pure export.

---

#### TD-D8 — one declaration per shape

13. **Import the lib types instead of shadowing them.** Five components. Each
    edit is: delete the local `interface`, import the lib type (narrowing with
    `Pick<>` where the component deliberately reads less), keep every docblock
    on the local declaration by moving it above the import or onto the props.

    13a. `landing/src/components/dashboard/analytics-cards.tsx:24-32`.

    Before:

    ```ts
    interface SummaryView {
      posted: number;
      skipped: number;
      rejected: number;
      failed: number;
      total: number;
      success_rate: number | null;
      avg_per_day: number | null;
    }
    ```

    After:

    ```ts
    import type { SummaryView } from "@/lib/dashboard-payloads";
    ```

    (at the top with the other imports; the docblock above `:24` stays where it
    is, immediately above the `Unavailable` explanation it introduces.)

    13b. `landing/src/components/dashboard/category-breakdown.tsx:28-34` →
    `import type { CategoryView } from "@/lib/dashboard-payloads";`.

    The local docblock says `configured_ratio` is `number | null` "rather than
    optional so that a future edit cannot reintroduce the silent version with
    `?? 0`". The lib type spells it `number | Unavailable` where
    `Unavailable = null` — the same type, with the name that carries the reason.
    Keep the docblock, adding one clause: `— the lib type spells the null
    \`Unavailable\`, which is the same type under the name that says why.`

    13c. `landing/src/components/dashboard/media/pool-health.tsx:18-25` →
    `import type { PoolHealthView } from "@/lib/dashboard-payloads";`.

    13d. `landing/src/components/dashboard/posting-chart.tsx:26-30`.

    Before:

    ```ts
    interface DayCount {
      local_date: string;
      count: number;
      cap: number;
    }
    ```

    After:

    ```ts
    import type { StatsResponse } from "@/lib/dashboard-payloads";

    /** One bar: a row of `stats.posts_by_day`, which is where the cap comes from. */
    type DayCount = StatsResponse["posts_by_day"][number];
    ```

    13e. `landing/src/components/dashboard/recent-activity.tsx:16-22`.

    Before:

    ```ts
    interface ActivityItem {
      id: string;
      state: string;
      file_name: string;
      category: string | null;
      entered_state_at: string;
    }
    ```

    After:

    ```ts
    import type { Intent } from "@/lib/intents";

    /**
     * The five columns this list reads, NARROWED from the intent row rather than
     * re-declared. `Pick` is the point: the component says what it needs, the
     * compiler says whether the row still has it, and a column renamed on the
     * server fails here instead of rendering `undefined`.
     */
    type ActivityItem = Pick<
      Intent,
      "id" | "state" | "file_name" | "category" | "entered_state_at"
    >;
    ```

    `dashboard/page.tsx:70` passes `historyResult.data.intents` (now `Intent[]`)
    straight into `items` — assignable.

    Importer greps for the step:

    ```bash
    grep -rn "interface SummaryView\|interface CategoryView\|interface PoolHealthView\|interface DayCount\|interface ActivityItem" landing/src
    ```

    Expected **before**: 5 hits, one per component.
    Expected **after**: 0 hits.

    ```bash
    grep -rn "SummaryView\|CategoryView\|PoolHealthView" landing/src
    ```

    Expected **after**: each name appears in `dashboard-payloads.ts` (the
    declaration and its `deriveX` return type) and in the one component that
    imports it. No third site.

    Tests: `dashboard-payloads.test.ts` and `settings-view.test.ts` already
    cover the `deriveX` functions and must stay green unchanged. No new test —
    the value of this step is a compile error, not an assertion.

14. **Delete the second `Workspace`** — `landing/src/app/api/workspaces/route.ts:6-11`.

    Before:

    ```ts
    export type Workspace = {
      id: string;
      name: string;
      role: "owner" | "admin" | "member";
      state: "active" | "suspended" | "offboarding";
    };
    ```

    After:

    ```ts
    import type { Workspace } from "@/lib/workspaces";
    ```

    (added to the import block at `:1-4`; the type is used at `:18` in
    `targetFetch<{ workspaces: Workspace[] }>`.)

    Importer grep:

    ```bash
    grep -rn "export type Workspace" landing/src
    ```

    Expected **before**: 2 hits (`api/workspaces/route.ts:6`,
    `lib/workspaces.ts:4`). Expected **after**: 1 hit (`lib/workspaces.ts:4`).

    ```bash
    grep -rn 'api/workspaces/route"' landing/src
    ```

    Expected **before and after**: 0 hits — nothing imports the route module's
    type, so removing the `export` breaks nobody. (Route modules are not import
    targets in the App Router; exporting a type from one was the accident.)

15. **One badge tone, one class map** — new file
    `landing/src/components/dashboard/tone.ts`.

    The union `"active" | "attention" | "inert"` is declared three times
    (`lib/destination.ts:61-64`, `lib/drive.ts:61-64`,
    `api-tokens-tab.tsx:91-94`) and the class map twice
    (`accounts-tab.tsx:54-58`, `api-tokens-tab.tsx:112-116`) plus once as a
    ternary (`integrations-tab.tsx:556-562`). Three declarations and three
    maps: past the rule of three, extract.

    New file:

    ```ts
    /**
     * The dashboard's badge vocabulary: three tones and their Tailwind.
     *
     * SEMANTICS AND CLASSES ARE STILL SPLIT, and that split is the reason this
     * file is in `components/` rather than `lib/`. `lib/destination.ts`,
     * `lib/drive.ts` and `api-tokens-tab.tsx` each decide which tone a state
     * DESERVES — a pure question, unit-tested without a DOM, and each keeps
     * its own `Record` keyed on its own closed state set so a state without a
     * badge stays a compile error. What they no longer each own is the tone
     * union itself and the tone→class map, which are one visual language and
     * were written out three times and two-and-a-half times respectively.
     *
     * THE INVARIANT THE COPIES EACH RESTATED: only `active` is ever green.
     * `attention` is amber because something is asking to be looked at;
     * `inert` is the muted pair rather than a third colour, because `disabled`
     * and `moved` (and `revoked` and `expired`) differ in their LABEL, not in
     * kind, and inventing a colour per state would claim otherwise.
     */
    export type BadgeTone = "active" | "attention" | "inert";

    export const TONE_CLASS: Record<BadgeTone, string> = {
      active: "bg-green-100 text-green-800",
      attention: "bg-amber-100 text-amber-900",
      inert: "bg-muted text-muted-foreground",
    };
    ```

    The three class strings are copied verbatim from
    `accounts-tab.tsx:55-57` / `api-tokens-tab.tsx:113-115`, which are already
    byte-identical to each other and to `integrations-tab.tsx`'s ternary arms.
    Confirm before writing:

    ```bash
    grep -rn "bg-green-100 text-green-800\|bg-amber-100 text-amber-900\|bg-muted text-muted-foreground" landing/src
    ```

    Expected **before**: 3 sites × 3 classes in the tone maps/ternary, plus
    unrelated one-off uses of `bg-green-100 text-green-800` in
    `recent-activity.tsx`'s `statusVariant` map (leave that one — it is keyed
    on intent state, not on tone, and is a different question).

    Then:

    - `lib/destination.ts:61-64` — `tone: "active" | "attention" | "inert";`
      becomes `tone: BadgeTone;` with `import type { BadgeTone } from "@/components/dashboard/tone";`.
    - `lib/drive.ts:61-64` — same edit on `DriveBadge`.
    - `api-tokens-tab.tsx:91-94` — same edit on `TokenBadge`; delete the local
      `TONE_CLASS` at `:112-116` and import it instead; `:391`
      (`className={TONE_CLASS[badge.tone]}`) is unchanged.
    - `accounts-tab.tsx:44-58` — delete `STATE_TONE_CLASS` and its docblock,
      import `TONE_CLASS`, and change `:195` from
      `className={STATE_TONE_CLASS[stateBadge.tone]}` to
      `className={TONE_CLASS[stateBadge.tone]}`.
    - `integrations-tab.tsx:553-563` — replace the ternary:

      Before:

      ```tsx
      {/* GREEN BELONGS TO THE GRANT ALONE: `driveStatusBadge` is
          pinned so only `active` ever carries this tone. */}
      <Badge
        variant="secondary"
        className={
          grant.tone === "active"
            ? "bg-green-100 text-green-800"
            : grant.tone === "attention"
              ? "bg-amber-100 text-amber-900"
              : "bg-muted text-muted-foreground"
        }
      >
      ```

      After:

      ```tsx
      {/* GREEN BELONGS TO THE GRANT ALONE: `driveStatusBadge` is
          pinned so only `active` ever carries this tone. The map is
          `components/dashboard/tone.ts` now — this was the third copy,
          and the only one written as a ternary, which is how it could
          have disagreed without a compile error. */}
      <Badge variant="secondary" className={TONE_CLASS[grant.tone]}>
      ```

    > **`lib/` importing from `components/`.** `destination.ts` and `drive.ts`
    > take a *type-only* import (`import type`), which is erased at compile time
    > and creates no runtime edge. This is the one direction exception and it is
    > deliberate: putting the union in `lib/` would put a Tailwind decision in
    > the layer whose whole docblock argument is that it carries no Tailwind.
    > If a reviewer objects, the alternative is `lib/badge-tone.ts` holding only
    > `BadgeTone`, with `TONE_CLASS` in `components/dashboard/tone.ts`. Either
    > is acceptable; do not invent a third.

    Importer greps:

    ```bash
    grep -rn '"active" | "attention" | "inert"' landing/src
    ```

    Expected **before**: 3 hits. Expected **after**: 1 hit (`tone.ts`).

    ```bash
    grep -rn "STATE_TONE_CLASS\|TONE_CLASS" landing/src
    ```

    Expected **before**: 4 hits (`accounts-tab.tsx:54,195`,
    `api-tokens-tab.tsx:112,391`).
    Expected **after**: 5 hits — the declaration in `tone.ts`, two imports, and
    the three use sites (`accounts-tab.tsx:195`, `api-tokens-tab.tsx:391`,
    `integrations-tab.tsx`).

    Tests: `destination.test.ts`, `drive.test.ts` and
    `destination-badge-contract.test.ts` must stay green with no edit — the
    tone values do not change, only where the union is declared.

    **New pin.** Add `landing/src/components/dashboard/tone.test.ts`, a pure
    export test (no DOM), asserting the invariant the three copies each
    restated in prose and none enforced:

    ```ts
    import { describe, expect, it } from "vitest";
    import { TONE_CLASS, type BadgeTone } from "./tone";
    import { destinationStateBadge } from "@/lib/destination";
    import { driveStatusBadge } from "@/lib/drive";
    import { tokenStateBadge } from "@/components/dashboard/settings/api-tokens-tab";

    /**
     * GREEN IS A CLAIM, and three files used to make it in prose only.
     *
     * Each of `destination.ts`, `drive.ts` and `api-tokens-tab.tsx` carried a
     * comment saying "only `active` is ever green", and each owned its own copy
     * of the class map, so the sentence was true by coincidence three times.
     * The map is one object now; this is the assertion that the sentence was
     * describing.
     */
    describe("the badge tone vocabulary", () => {
      it("gives green to `active` and to nothing else", () => {
        const green = (tone: BadgeTone) => TONE_CLASS[tone].includes("green");
        expect(green("active")).toBe(true);
        expect(green("attention")).toBe(false);
        expect(green("inert")).toBe(false);
      });

      it("has a class for every tone", () => {
        for (const tone of ["active", "attention", "inert"] as const) {
          expect(TONE_CLASS[tone], tone).toBeTruthy();
        }
      });
    });
    ```

    > Drop the three imports of `destinationStateBadge`/`driveStatusBadge`/
    > `tokenStateBadge` if `api-tokens-tab.tsx` cannot be imported in the `node`
    > environment (it is a `"use client"` React module). Check first: the file
    > already exports `tokenStateBadge` as "a hook-free exported block for its
    > test", so an existing test imports it. If no such test exists, write the
    > case against `TONE_CLASS` alone — that is the extraction this step makes,
    > and it is what needs pinning.

16. **One UUID predicate** — `landing/src/lib/commands.ts:57-63` adopts
    `lib/session.ts`'s.

    `session.ts:241` already calls itself "The one UUID-shape predicate" and is
    the one five route files use. `commands.ts` has a second, stricter one
    (RFC-4122 v1–5: version nibble `[1-5]`, variant nibble `[89ab]`) with one
    importer — itself.

    **The choice, made once here:** keep the **loose** `isUuid` and delete
    `isUuidLike`. Reasons: (a) it is the one with five importers and the
    docblock claiming singularity; (b) `session-guards.test.ts:125-131` already
    pins it, including the all-zeros id at `:59` which the strict regex
    **rejects** — and `00000000-0000-0000-0000-000000000000` is a legal
    workspace id shape this tier must forward; (c) the strictness buys nothing:
    Postgres `gen_random_uuid()` emits v4, so every real id passes both, and
    this predicate's job is to stop a junk cookie becoming a junk path segment,
    not to validate a version nibble the API re-checks anyway.

    Before (`commands.ts:57-63`):

    ```ts
    const UUID_RE =
      /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;

    export function isUuidLike(v: unknown): v is string {
      return typeof v === "string" && UUID_RE.test(v);
    }
    ```

    After:

    ```ts
    import { isUuid } from "./session";
    ```

    (at the top of `commands.ts`) and every `isUuidLike(` call site in the file
    becomes `isUuid(`.

    Add one sentence to `session.ts:241`'s docblock recording that the choice
    was made rather than defaulted:

    ```ts
    /**
     * The one UUID-shape predicate: every id this tier forwards to the API is
     * one or nothing.
     *
     * DELIBERATELY SHAPE-ONLY, not RFC-4122. `commands.ts` carried a second,
     * stricter copy (`isUuidLike`, version nibble `[1-5]`, variant `[89ab]`)
     * until #NNNN. Postgres `gen_random_uuid()` emits v4, so every id this tier
     * will ever see passes both — and the strict form REJECTS
     * `00000000-0000-0000-0000-000000000000`, which is a legal id shape and is
     * pinned in `session-guards.test.ts`. The job here is to stop a junk cookie
     * becoming a junk path segment; the API re-validates everything it is sent.
     */
    ```

    Importer greps:

    ```bash
    grep -rn "isUuidLike" landing/src
    ```

    Expected **before**: hits only in `commands.ts` (the declaration and its
    call sites). Expected **after**: 0 hits.

    ```bash
    grep -rn "UUID_RE" landing/src
    ```

    Expected **before**: 2 hits in `commands.ts`. Expected **after**: 0 hits.

    Tests: `commands.test.ts` must stay green. If it asserts that a
    non-RFC-4122 string is refused, that case is now wrong — **update the case
    and say so in the diff**; the all-zeros id and any 8-4-4-4-12 hex string are
    now accepted by `commands.ts` exactly as they already were by every route
    handler. Run it alone:

    ```bash
    npx --prefix landing vitest run src/lib/commands.test.ts src/lib/session-guards.test.ts
    ```

---

17. **CHANGELOG** — `CHANGELOG.md`, under `## [Unreleased]`. Add one bullet to
    `### Removed` and one to `### Changed` (create `### Removed` above
    `### Changed` if it is not present; the `Added`/`Changed`/`Removed`/`Fixed`
    order is Keep-a-Changelog's). Substitute the real PR number for `#NNNN`
    everywhere it appears in this document before committing.

    Under `### Removed`:

    ```markdown
    - **The dashboard BFF proxy and the control that kept it mounted (#NNNN).** `/api/dashboard/[...path]` was an authenticated door forwarding to `/api/v1/workspaces/{ws}/<path>` behind a 25-entry allowlist; of those, `accounts`, `category-mix` and `media` are served by the API and already have dedicated routes on the site, and the other twenty-two — `init`, `analytics`, `toggle-setting`, `switch-account`, `queue-detail`, `disconnect-gdrive` and the rest — are served by nothing, so any session could POST them and get a 404 relayed as a 502. Its only caller was `postApi("switch-account")` behind the Accounts tab's "Make Active" button, which `settings/page.tsx` hard-coded to `editable={false}` — a control that could not be pressed and a second wire dialect beside the command client, which `commands.ts` names as the original defect. The route, `lib/dashboard-api.ts`, `switchAccount`, `DISABLED_REASON`, the read-only footnote and the `editable` prop are all gone; account switching comes back as a `switch_account` row in `COMMAND_SPECS` when the API offers one (epic P6). With them: `jose` (no importer since the session stopped being a self-signed JWT), `InstagramAccount` and `Instance` in `lib/types.ts`, `SetupState`/`InitResponse` and the "the screen stays on `init`" paragraph that `deriveSettings` had already contradicted, `formatLastPost`, `components/ui/{progress,slider}.tsx`, `telegramLinkedFrom` (kept alive only by its own test; `session.ts` inlines the same expression), and the `/login` Content-Security-Policy override that granted `'unsafe-inline' 'unsafe-eval' https://telegram.org` for a login widget deleted with the Telegram-rooted tier — and which, by omitting `plausible.io`, was blocking the site's own analytics script on that one page. The Analytics sidebar entry is gone too, under the sidebar's own rule that a nav item is a promise a destination exists; the page it pointed at, which renders one "Coming Soon" card, stays reachable by URL rather than becoming a 404 for anyone holding the link.
    ```

    Under `### Changed`:

    ```markdown
    - **One owner per contract on the web tier (#NNNN).** `GET …/intents` was typed twice — `Intent`/`IntentsResponse` in `lib/intents.ts` and `IntentRow`/a second `IntentsResponse` in `lib/dashboard-payloads.ts` — and the copies had already drifted: the second was missing `account_handle` and `account_display_name`, which `_INTENT_COLUMNS` has served since `06` §3, and typed `ig_account_id`, `schedule_slot_at` and `approval_mode` as nullable where the server never sends null. Structural typing kept both compiling. `intents.ts` now owns the row, the envelope and the state arrays; `dashboard-payloads.ts` re-exports them and derives its `?state=` query spellings as `NON_TERMINAL_STATES.join(",")` and `TERMINAL_STATES.join(",")`, so a state added server-side reaches all three screens or none — and `intent-states-contract.test.ts`, which reads the API's own `INTENT_STATES` tuple, now pins the arrays and the derived strings together rather than one hand-written partition. Five dashboard components stopped shadowing the payload types they render (`SummaryView`, `CategoryView`, `PoolHealthView`, the `posts_by_day` row, and the five columns Recent Activity reads, now a `Pick` of the intent row), `Workspace` is declared once instead of twice, the badge tone union and its Tailwind map are one `components/dashboard/tone.ts` instead of three unions and two-and-a-half maps — with a test for the invariant all three restated in prose, that only `active` is ever green — and `commands.ts`'s second UUID predicate is gone in favour of `session.ts`'s, which is the one five route handlers use and the one that accepts the all-zeros id the strict RFC-4122 form rejected.
    ```

## Test Plan

- **Pre-change baseline**, on the parent commit (`0966771`), from the repo root:

  ```bash
  npm --prefix landing run test
  npx --prefix landing tsc --noEmit
  npm --prefix landing run lint
  ```

  Green means: vitest reports every file passing with no `skipped` contract
  test (the `intent-states-contract`, `wire-contract`, `session-cookie-contract`
  and `destination-badge-contract` files fail loudly rather than skip when they
  cannot read their counterparty — a skip is a failure here); `tsc` prints
  nothing; eslint prints nothing. `git status` clean after.

- **Targeted**, run after the steps they cover:

  | Step | Command |
  |---|---|
  | 4, 6, 7 | `npx --prefix landing vitest run src/lib/intent-states-contract.test.ts src/lib/intents.test.ts src/lib/dashboard-payloads.test.ts src/lib/settings-view.test.ts` |
  | 8 | `npx --prefix landing vitest run src/lib/destination-badge-contract.test.ts src/lib/destination.test.ts` |
  | 11 | `npx --prefix landing vitest run src/lib/telegram-link.test.ts` |
  | 15 | `npx --prefix landing vitest run src/components/dashboard/tone.test.ts src/lib/drive.test.ts src/lib/destination.test.ts` |
  | 16 | `npx --prefix landing vitest run src/lib/commands.test.ts src/lib/session-guards.test.ts` |

- **New/updated pins:**
  - `landing/src/lib/intent-states-contract.test.ts` — **new case**
    `"keeps the query spellings derived from the arrays, not re-typed"`: asserts
    `split(QUEUE_STATES) === [...NON_TERMINAL_STATES]`,
    `split(TERMINAL_STATES) === [...TERMINAL_STATES from intents]`, and that
    their union is exactly `INTENT_STATES`. This is what stops the comma strings
    becoming a second partition again.
  - `landing/src/components/dashboard/tone.ts` + **new file**
    `landing/src/components/dashboard/tone.test.ts` — asserts only `active`
    carries a green class, and that every tone has one. Pure export, no DOM.
  - `landing/src/lib/intents.test.ts` — comment only, recording why the
    hand-typed literal survives as an ordering check.
  - `landing/src/lib/telegram-link.test.ts` — the `telegramLinkedFrom` describe
    block deleted with the export.
  - `landing/src/lib/commands.test.ts` — updated only if it asserts that a
    non-RFC-4122 UUID string is refused (see step 16).
  - `landing/src/lib/destination-badge-contract.test.ts` — comment only; the
    assertion is untouched.

- **Post-change**, from the repo root:

  ```bash
  npm --prefix landing run test
  npx --prefix landing tsc --noEmit
  npm --prefix landing run lint
  npm --prefix landing run build
  ```

  `build` is in this PR's gate specifically because it deletes a route and two
  components: a stale import that `tsc` resolves through a path alias but Next
  cannot bundle shows up here and nowhere else.

## Verification Checklist

- [ ] baseline green on the parent commit (`0966771`): `test`, `tsc --noEmit`, `lint` all clean, `git status` clean
- [ ] importer audit: every grep in steps 2–16 run, results exactly as stated (record the before/after counts in the PR body)
- [ ] `grep -rn "IntentRow" landing/src` returns 0
- [ ] `grep -rn "api/dashboard" landing/src` returns 0
- [ ] `grep -rn "isUuidLike\|UUID_RE" landing/src` returns 0
- [ ] `grep -rn '"active" | "attention" | "inert"' landing/src` returns exactly 1 (`tone.ts`)
- [ ] `QUEUE_STATES` and `TERMINAL_STATES` render byte-identical strings to the parent commit (the new contract-test case proves it; eyeball the diff too)
- [ ] `npm --prefix landing run test` green
- [ ] `npx --prefix landing tsc --noEmit` clean
- [ ] `npm --prefix landing run lint` clean
- [ ] `npm --prefix landing run build` succeeds
- [ ] manual smoke — `/dashboard`: Recent Activity lists the same rows with the same badges; `/dashboard/media/calendar`: all three lanes render and the three stat cards read the same as before (the "Posting Rate" card is **untouched** by this PR and may still say "interval not set" on a wrapping window — that is D2, ruled on separately); `/dashboard/settings` › Accounts: the rows render with their state badges and their Connect/Reconnect/Remove buttons, and there is no "Make Active" button and no read-only footnote; `/dashboard/settings` › API tokens and › Integrations: the badges are the same colours; sidebar: five items, no Analytics; `/dashboard/analytics` typed directly still answers with its "Coming Soon" card; `/login`: renders and hydrates, the Google button works, DevTools › Console shows no CSP violation and (with `NEXT_PUBLIC_PLAUSIBLE_DOMAIN` set) `plausible.io/js/script.js` loads
- [ ] `CHANGELOG.md` entry added under `## [Unreleased]`, `#NNNN` replaced with the real PR number here and in every docblock that cites it
- [ ] nothing in "What NOT To Do" happened

## What NOT To Do

These change what a person sees. Each needs its own ruling or its own bug-fix
PR; none of them rides inside this cleanup.

- **Do NOT touch the Calendar's posting-window arithmetic (D2).**
  `media/calendar/page.tsx:110-118` computes `config.posting_hours_end -
  config.posting_hours_start` and guards on `windowHours > 0`, while
  `lib/schedule.ts:17-18` mirrors `fn_next_slot` (start = end ⇒ 24 h, start >
  end ⇒ wraps midnight). Sharing the helper **changes the rendered figure** on a
  22→02 or a 24-hour window. It is question 9 in `00_TECH_DEBT.md`. This PR
  edits imports and one type annotation in that file and nothing else; the
  `windowHours`/`intervalMinutes` block is left exactly as found.
- **Do NOT fix the mobile navigation (D11's `mobile` prop).**
  `sidebar.tsx:38-43` takes a `mobile` prop nobody passes, and the header's
  `lg:hidden` trigger disagrees with the aside's `md:block`. Passing the prop
  **makes the drawer render** where it renders empty today. Question 10. Step 12
  edits `navItems` and the `lucide-react` import in that file and nothing else —
  do not touch the `mobile` parameter, the `visibleItems` line or the
  `className` ternary while you are in there. (`visibleItems` is doc 15's.)
- **Do NOT remove the fabricated settings defaults (D10).** `?? 30` / `?? 45` at
  `repost-cadence-card.tsx:32-37`, `?? "enhanced"` at `caption-style-card.tsx:40`.
  Rendering `<Unavailable/>` instead **changes the number on the card**.
  Question 11.
- **Do NOT delete `app/(dashboard)/dashboard/analytics/page.tsx`.** Step 12
  removes the nav item and keeps the page on purpose; deleting it turns a 200
  into a 404.
- **Do NOT tighten the `/login` CSP to `script-src 'self'`** instead of removing
  it. The App Router's inline hydration scripts need `'unsafe-inline'` or a
  nonce; `'self'` alone breaks the page. Remove the override.
- **Do NOT delete `destinationIsActive` from `lib/destination.ts`**, even though
  step 8 removes its last non-test caller. It is pinned by `destination.test.ts`
  and P6 brings the caller back.
- **Do NOT change the `destination-badge-contract.test.ts` assertion** in step
  8e. The comment is stale; the regex is the tripwire.
- **Do NOT reword any 401/400/422 error string** anywhere in this PR. The
  browser's refusal tables switch on that vocabulary. (This is doc 15's central
  constraint; it applies here too, in the deleted proxy's neighbours.)
- **Do NOT run `npm run build` or `npm install` beyond step 10's single
  `npm uninstall jose`** while drafting; the verification pass runs them once at
  the end.
- **Do NOT touch anything outside `landing/` and `CHANGELOG.md`.** This PR reads
  `src/services/target/workspaces.py` and `src/services/target/vocabulary.py`
  (the contract tests do) and writes neither.

## Related

- `00_TECH_DEBT.md` — the audit; row 14 of "Remediation order and dependency
  matrix", and questions 9, 10 and 11 in "Questions", which are the three
  exclusions above.
- `15_landing-shared-shapes.md` — the next PR; depends on this one.
- `16_landing-integrations-tab.md` — depends on 15.
- `AGENTS.md` §"Architecture" — the UI layer rule: the site calls the API only
  through `landing/src/lib/target-api.ts`, and the one table it owns is the
  marketing waitlist. Deleting the BFF proxy removes the last exception to the
  first half of that rule (the two sign-in handoff anchors in
  `google-login-button.tsx:36` and `join/[token]/start/route.ts:29` are
  deliberate and stay).
- `.claude/rules/changelog.md` — the entry format; CI's `changelog-check` gates
  any PR touching code.
- **Follow-ups this PR deliberately does not take:** delete
  `app/(dashboard)/dashboard/analytics/page.tsx` outright (a product ruling on
  whether Phase 3 analytics is still planned); drop the two redundant
  `as string` casts at `media/calendar/page.tsx:85,90`; decide whether
  `lib/telegram.ts` should hold `TELEGRAM_BOT_TOKEN` in the site's environment
  at all (Observations, `00_TECH_DEBT.md`).

## Origin

`/artemis-skills:audit tech debt`, 2026-09-20, on `main` at `0966771`.
Findings TD-D1, TD-D3, TD-D4 and TD-D8 from
`tech-debt-2026-09-20/research/landing.md` (scratch, not committed). Every
`path:line` in this plan was re-read at `0966771`; the `_INTENT_COLUMNS` drift
and the 22 dead allowlist entries were verified against
`src/services/target/workspaces.py:381-386` and `src/api/routes/v1.py`
respectively, and are quoted in "Findings addressed".
