---
title: "The web tier's repeated shapes, named constants, dead props and misplaced docblocks"
type: plan
status: draft
owner: chris
created: 2026-09-20
tags: [plan, tech-debt, priority:medium, landing]
links: []
---

# 15 — The web tier's repeated shapes, named constants, dead props and misplaced docblocks

| | |
|---|---|
| **PR title** | The web tier's repeated shapes, named constants, dead props and misplaced docblocks |
| **Risk** | Medium — it touches 18 route handlers, 5 pages and 8 components; every edit is mechanical, but the 401/400 vocabulary it moves is a contract the browser's refusal tables switch on |
| **Effort** | M (≈8–10 h) |
| **Files modified** | **New**: `landing/src/lib/bff.ts`, `landing/src/lib/route-guards.ts`, `landing/src/lib/page-guards.ts`, `landing/src/lib/workspace-name.ts`, `landing/src/components/ui/notice.tsx`, `landing/src/lib/bff.test.ts`, `landing/src/lib/route-guards.test.ts`, `landing/src/components/ui/notice.test.ts`. **Edited**: `landing/src/lib/{tokens,start-grant,command-client,drive,telegram-link,category-mix,intents,session,commands,refusal-copy,target-api}.ts`, all 18 route handlers under `landing/src/app/api/` plus `landing/src/app/join/[token]/start/route.ts`, the 5 dashboard pages, `landing/src/components/dashboard/{sidebar,header}.tsx`, `landing/src/components/dashboard/queue/queue-list.tsx`, `landing/src/components/workspace/{create-workspace-form,accept-invitation,workspace-list}.tsx`, `landing/src/components/dashboard/settings/{general-tab,caption-style-card,repost-cadence-card,category-weights-card,accounts-tab,api-tokens-tab,integrations-tab,members-card,danger-zone-card}.tsx`, `landing/src/components/workspace/router-unavailable.tsx`, `landing/src/app/welcome/page.tsx`, `landing/src/app/auth/error/{page.tsx,content.ts}`, `CHANGELOG.md` |
| **Findings addressed** | TD-D5, TD-D6, TD-D9, TD-D10 (constants only), TD-D11 (dead props only), TD-D12 |
| **Depends on** | 14 (`14_landing-one-contract.md`) |
| **Blocks** | 16 (`16_landing-integrations-tab.md`) |

## Summary

Six findings, one theme: a shape written out so many times that the copies have
started to disagree about the details.

The browser's `fetch` → `{ok, error, status}` wrapper is hand-written **nine
times** across `lib/`, plus four raw variants in components. A `lib/bff.ts`
with `callBff` + `postJson` gives it one home; each caller keeps its own
post-validation (a Drive folder list, a Telegram link, an authorization URL),
which is the part that genuinely differs.

Nineteen route handlers open with the same `getSessionToken()` → 401 block
(eighteen after doc 14 deletes the BFF proxy), thirteen repeat the
`isWorkspaceId` → `invalid_workspace`/400 check, sixteen repeat the `!result.ok`
pass-through, six the `request.json()` → `malformed_body` try/catch; five pages
repeat a four-line redirect preamble, four of them with the comment pasted
verbatim. `lib/route-guards.ts` and `lib/page-guards.ts` collect them. **No
error string changes.** That vocabulary is what `refusalCopy`,
`settingsRefusalCopy`, `createWorkspaceRefusalCopy`, `destinationConnectRefusalCopy`
and `driveRefusalCopy` `switch` on; one route answering `unauthorised` instead
of `unauthenticated` would silently miss every `case` and fall to a generic
sentence. The guards exist to make that vocabulary un-mistypeable, not to
revise it.

Sixteen inline notice banners become `<Notice>`, with `role="alert"` on the
five error boxes that were missing it — an accessibility improvement with no
visual change.

Then the named constants (a workspace-name max that two sites spell `100` and
one spells `120`; a Telegram link TTL that three sites spell as `900` seconds
and one as `15` minutes; four inline page limits; a cookie name exported from a
route module), the props no call site passes, and ten docblocks that sit above
the wrong symbol.

**Three things are deliberately out.** The Calendar's posting-window arithmetic
(D2), the mobile-navigation breakpoint fix (D11's `mobile` prop) and the
settings cards' fabricated `30`/`45`/`"enhanced"` defaults (D10) each change
what a person sees. They are questions 9, 10 and 11 in `00_TECH_DEBT.md` and
they are named again in "What NOT To Do".

## Findings addressed

| ID | Where | One line |
|---|---|---|
| TD-D5 | `lib/tokens.ts:229-245` (the owner-shaped copy) + 8 more in `lib/`, 4 raw in components | One fetch→result wrapper, hand-written nine times; the queue also re-literals the command path. |
| TD-D6 | 18 route handlers, 5 dashboard pages | The session/workspace/body/pass-through preamble copy-pasted; four pages carry the same comment verbatim. |
| TD-D9 | 16 banner sites across 8 components + `settings/page.tsx` | `role="alert"` on 4 of 9 error boxes, two green shades, one dismiss control. |
| TD-D10 *(constants only)* | `lib/workspaces.ts`/`create-workspace-form.tsx:118`, `lib/telegram-link.ts`, `dashboard/page.tsx:38`, `media/calendar/page.tsx:45,49,53`, `join/[token]/start/route.ts:5`, `command-client.ts:196,232` | Magic numbers with a name available, a cookie name exported from a route module, a sentence retyped beside the helper that returns it. |
| TD-D11 *(dead props only)* | `header.tsx:31-35,60`, `create-workspace-form.tsx:25`, `sidebar.tsx:40`, `general-tab.tsx:182,192`, `caption-style-card.tsx:19`, `repost-cadence-card.tsx:13`, `category-weights-card.tsx:43-47` | Props no call site passes, flags that are always `true`, and state derived from props through an effect. |
| TD-D12 | `command-client.ts:115-124`, `commands.ts:113-120,181-192,239-248,256-260`, `refusal-copy.ts:50-57`, `general-tab.tsx:88-94,253-260`, `integrations-tab.tsx:282-292`, `auth/error/page.tsx:16-30`, plus 8 stale claims | Ten docblocks stacked above the wrong symbol, and comments that now state the opposite of the code. |

### Withdrawn on re-reading

| ID | Item | Why |
|---|---|---|
| TD-D11 | **`general-tab.tsx:140` `inertReason` is NOT dead.** | The research reads it as a prop "set by no row", which is true of the `TOGGLES` data and false of the code. `inertReason` is declared on `ToggleRow` (`:140`), read by the exported `isLiveToggle` (`:121`), rendered at `:591-593`, and **pinned by two cases in `general-tab-toggles.test.ts`** — `"every toggle that is not live says why, in its own words"` (`:58-62`) and `"no inert reason names a cause the code did not establish"` (`:95-105`), which assert a minimum length and forbid the words "error" and a cause the code did not establish. The docblock at `:76-82` explains that `inertReason` answers "why does this switch not move". Deleting it would delete the mechanism that forces the *next* inert row to explain itself, and would gut two tests. **Keep it, unchanged.** No row setting it today is the healthy state, not the dead one. |
| TD-D12 | **`setup/connect/page.tsx:101-104` is out of scope.** | The research's path is wrong (`landing/src/app/(marketing)/setup/connect/page.tsx`), and more importantly it is **rendered marketing copy**, not a docblock: `<Callout type="info">Connecting an Instagram account is not available from the dashboard yet…</Callout>`. Changing it changes what a visitor reads. `00_TECH_DEBT.md` §Observations already routes marketing-copy drift to whoever owns copy, alongside `config/faqs.ts:8-11,23-26`. Left for that ruling. |
| TD-D12 | **`dashboard-payloads.ts:465-484`** ("the screen stays on `init`") | Already deleted by doc 14, step 11, together with the `SetupState`/`InitResponse` types it introduced. Nothing to do here. |
| TD-D12 | **`accounts-tab.tsx:37-39`** | Already rewritten by doc 14, step 8c, when `DISABLED_REASON` went. Nothing to do here. |

Everything else in the table was re-read at `0966771` and matched its cited
`path:line`.

## Dependencies

- **Depends on: doc 14.** Three hard reasons, not just tidiness:
  1. Doc 14 deletes `app/api/dashboard/[...path]/route.ts`, which is one of
     D6's nineteen preamble routes. Doing D6 first would migrate a file that is
     about to be deleted.
  2. Doc 14 removes the `editable` prop from `AccountsTab`; this doc removes it
     from `GeneralTab`, `CaptionStyleCard` and `RepostCadenceCard`. Both touch
     `settings/page.tsx`'s `editable`-is-per-tab essay.
  3. Doc 14 moves the badge tone map out of `accounts-tab.tsx` and
     `api-tokens-tab.tsx`; this doc edits the banners in the same two files.
- **Blocks: doc 16**, which decomposes `integrations-tab.tsx`. That file gets a
  `<Notice>`, a `callBff` migration and two docblock moves here; doing 16 first
  would split those edits across the new sub-components.
- **Parent commit:** doc 14's merge commit.

## Implementation Plan

### Steps

1. **Baseline** — on doc 14's merge commit, from the repo root:

   ```bash
   npm --prefix landing run test
   npx --prefix landing tsc --noEmit
   npm --prefix landing run lint
   ```

   All three green, `git status` clean. If doc 14 has not merged, stop: every
   step below assumes its deletions.

---

#### TD-D5 — one door for the browser's fetch

2. **Create `landing/src/lib/bff.ts`.** The body is `tokens.ts:229-245`'s
   `call`, promoted verbatim with one addition: the failure branch carries the
   parsed body, because `command-client.ts` reads a second key from it.

   ```ts
   /**
    * The browser's one door to this tier's own routes, and the shape every
    * caller answers in.
    *
    * NINE HAND-WRITTEN COPIES (#NNNN). `tokens.ts`, `start-grant.ts`,
    * `command-client.ts`, `drive.ts` (×3), `telegram-link.ts` (×2) and
    * `category-mix.ts` each carried this block: `try { fetch } catch {
    * unreachable/0 }`, then `json().catch(() => ({}))`, then
    * `typeof data?.error === "string" ? data.error : http_<status>`. Four
    * components carried rawer variants. They agreed by luck. A change to the
    * shape — a timeout, a non-JSON 502 body, a `Retry-After` — had nine places
    * to be missed.
    *
    * WHAT STAYS WITH THE CALLER, deliberately: everything after `ok`. A Drive
    * folder list that is not an array, a Telegram link that is not a `t.me`
    * URL, an authorization URL pointing at the wrong host — each of those is a
    * 200 that is still a failure, and each is a different question. This door
    * answers "did the call happen and did the route refuse it", nothing more.
    *
    * THE ERROR STRING IS A CONTRACT. `error` is whatever the route put in
    * `{"error": …}`, verbatim, or `http_<status>` when it put nothing there —
    * and `unreachable` with status 0 when `fetch` itself threw. Every refusal
    * table on this tier (`refusalCopy`, `settingsRefusalCopy`,
    * `createWorkspaceRefusalCopy`, `destinationConnectRefusalCopy`,
    * `driveRefusalCopy`) switches on those strings. Do not "improve" them here.
    */
   export type BffResult =
     | { ok: true; status: number; data: Record<string, unknown> }
     | { ok: false; error: string; status: number; body: Record<string, unknown> };

   /** One call to this tier's proxy: a typed result, never a throw. */
   export async function callBff(
     path: string,
     init?: RequestInit,
   ): Promise<BffResult> {
     let response: Response;
     try {
       response = await fetch(path, init);
     } catch {
       return { ok: false, error: "unreachable", status: 0, body: {} };
     }
     const data: unknown = await response.json().catch(() => ({}));
     const body =
       data && typeof data === "object" ? (data as Record<string, unknown>) : {};
     if (!response.ok) {
       const error =
         typeof body.error === "string" ? body.error : `http_${response.status}`;
       return { ok: false, error, status: response.status, body };
     }
     return { ok: true, status: response.status, data: body };
   }

   /**
    * A JSON request body. `method` defaults to POST because eight of the nine
    * callers post; `category-mix` is the PUT (a mix is a resource replaced
    * whole, not a command).
    */
   export function postJson(
     body: Record<string, unknown>,
     method: "POST" | "PUT" | "PATCH" = "POST",
   ): RequestInit {
     return {
       method,
       headers: { "Content-Type": "application/json" },
       body: JSON.stringify(body),
     };
   }
   ```

   > `body` on the failure branch is the one addition to `tokens.ts`'s `call`.
   > `tokens.ts` ignores it; `command-client.ts` needs it (step 4). Adding a
   > field to a union arm breaks no existing reader.

   **New pin** — `landing/src/lib/bff.test.ts`, a pure test with a stubbed
   `globalThis.fetch` (no DOM needed):

   ```ts
   import { afterEach, describe, expect, it, vi } from "vitest";
   import { callBff, postJson } from "./bff";

   /**
    * The shape nine modules used to hand-write. Each case here is a line one of
    * those copies could have got wrong on its own, and two of them had: the
    * non-JSON body and the thrown fetch are the paths that produce a blank
    * banner rather than a sentence.
    */
   const respond = (status: number, body: unknown, json = true) =>
     vi.spyOn(globalThis, "fetch").mockResolvedValue({
       ok: status >= 200 && status < 300,
       status,
       json: json ? async () => body : async () => { throw new Error("not json"); },
     } as unknown as Response);

   afterEach(() => vi.restoreAllMocks());

   describe("callBff", () => {
     it("returns the parsed body on success", async () => {
       respond(200, { link: "x" });
       const r = await callBff("/api/x");
       expect(r).toEqual({ ok: true, status: 200, data: { link: "x" } });
     });

     it("reads the route's own error string verbatim", async () => {
       respond(400, { error: "invalid_workspace" });
       const r = await callBff("/api/x");
       expect(r.ok).toBe(false);
       if (!r.ok) expect(r.error).toBe("invalid_workspace");
     });

     it("falls back to http_<status> when the route named no error", async () => {
       respond(403, { detail: "forbidden" });
       const r = await callBff("/api/x");
       if (!r.ok) expect(r.error).toBe("http_403");
     });

     it("keeps the refused body for the one caller that reads a second key", async () => {
       respond(409, { reason: "illegal_transition" });
       const r = await callBff("/api/x");
       if (!r.ok) expect(r.body.reason).toBe("illegal_transition");
     });

     it("survives a body that is not JSON at all", async () => {
       respond(502, null, false);
       const r = await callBff("/api/x");
       if (!r.ok) expect(r.error).toBe("http_502");
     });

     it("answers `unreachable` with status 0 when fetch throws", async () => {
       vi.spyOn(globalThis, "fetch").mockRejectedValue(new TypeError("network"));
       const r = await callBff("/api/x");
       expect(r).toEqual({ ok: false, error: "unreachable", status: 0, body: {} });
     });
   });

   describe("postJson", () => {
     it("posts by default and takes a method for the one PUT", () => {
       expect(postJson({ a: 1 })).toEqual({
         method: "POST",
         headers: { "Content-Type": "application/json" },
         body: '{"a":1}',
       });
       expect(postJson({ a: 1 }, "PUT").method).toBe("PUT");
     });
   });
   ```

3. **Migrate `tokens.ts`** — `landing/src/lib/tokens.ts:225-250`.

   Delete the local `WireResult` type, `call` and `postJson`; import them.

   Before:

   ```ts
   type WireResult =
     | { ok: true; status: number; data: Record<string, unknown> }
     | { ok: false; error: string; status: number };

   /** One call to this tier's proxy: a typed result, never a throw. */
   async function call(path: string, init?: RequestInit): Promise<WireResult> {
   ```

   After: the three declarations go, and the file gains

   ```ts
   import { callBff, postJson } from "./bff";
   ```

   Every `call(` in the file becomes `callBff(`. Verify:

   ```bash
   grep -n "call(\|postJson(\|WireResult" landing/src/lib/tokens.ts
   ```

   Expected **before**: the declarations plus each use site.
   Expected **after**: only `callBff(` and `postJson(` use sites; zero
   `WireResult`.

4. **Migrate `command-client.ts`** — `landing/src/lib/command-client.ts:64-100`.

   This is the one caller with a second error key, so it is written out in full.

   Before:

   ```ts
   export async function submitCommand(
     workspaceId: string,
     command: string,
     body: Record<string, unknown> = {},
   ): Promise<SubmitResult> {
     let response: Response;
     try {
       response = await fetch(`/api/workspaces/${workspaceId}/commands/${command}`, {
         method: "POST",
         headers: { "Content-Type": "application/json" },
         // The id rides in the body because that is where P2's `submissionCommand`
         // spec reads it; the route derives the header from it and the browser
         // never sets `Idempotency-Key` itself.
         body: JSON.stringify({ ...body, submission_id: crypto.randomUUID() }),
       });
     } catch {
       return { ok: false, error: "unreachable", status: 0 };
     }

     const data = await response.json().catch(() => ({}));

     if (!response.ok) {
       const error =
         typeof data?.error === "string"
           ? data.error
           : typeof data?.reason === "string"
             ? data.reason
             : `http_${response.status}`;
       return { ok: false, error, status: response.status };
     }

     if (data?.outcome === "replayed") {
       return { ok: false, error: REPLAYED_ERROR, status: response.status };
     }

     return { ok: true, data: data ?? {} };
   }
   ```

   After:

   ```ts
   /** The one path spelling for a command. The queue imports this rather than re-typing it. */
   export function commandPath(workspaceId: string, command: string): string {
     return `/api/workspaces/${workspaceId}/commands/${command}`;
   }

   export async function submitCommand(
     workspaceId: string,
     command: string,
     body: Record<string, unknown> = {},
   ): Promise<SubmitResult> {
     const result = await callBff(
       commandPath(workspaceId, command),
       // The id rides in the body because that is where P2's `submissionCommand`
       // spec reads it; the route derives the header from it and the browser
       // never sets `Idempotency-Key` itself.
       postJson({ ...body, submission_id: crypto.randomUUID() }),
     );

     if (!result.ok) {
       // THE ONE CALLER THAT READS TWO KEYS, spelled out rather than pushed
       // into `callBff`. The port's 4xx carry `{"error": …}` and its 409s carry
       // `{"reason": …}`; `callBff` reads the first and falls back to
       // `http_<status>`. Re-deriving here keeps the precedence identical to
       // what shipped (`error`, then `reason`, then the status) instead of
       // inferring it from the fallback string, which would misread a route
       // that legitimately answered `{"error": "http_409"}`.
       const error =
         typeof result.body.error === "string"
           ? result.body.error
           : typeof result.body.reason === "string"
             ? result.body.reason
             : `http_${result.status}`;
       return { ok: false, error, status: result.status };
     }

     if (result.data.outcome === "replayed") {
       return { ok: false, error: REPLAYED_ERROR, status: result.status };
     }

     return { ok: true, data: result.data };
   }
   ```

   with `import { callBff, postJson } from "./bff";` at the top.

   Run its test immediately — it is the most-covered of the nine:

   ```bash
   npx --prefix landing vitest run src/lib/command-client.test.ts
   ```

   Expected: green, unchanged.

5. **Migrate the six remaining `lib/` copies.** Each is the same edit: delete
   the `let response: Response; try { … } catch { … }` / `json().catch` /
   `!response.ok` block, call `callBff`, keep every line after `ok` untouched.

   | File | Lines | Call becomes |
   |---|---|---|
   | `lib/start-grant.ts` | `:17-30` | `await callBff(path, postJson({}))` — then the existing `authorizationUrl` type + host guard on `result.data` |
   | `lib/drive.ts` | `:132-145` | `await callBff(\`/api/workspaces/${workspaceId}/drive/folders${query}\`)` — no init, it is a GET |
   | `lib/drive.ts` | `:200-215` | `await callBff(path, postJson(body))` |
   | `lib/drive.ts` | `:271-287` | `await callBff(path, postJson(body))` |
   | `lib/telegram-link.ts` | `:42-56` | `await callBff("/api/me/telegram/link", postJson({}))` — then the existing `isTelegramLink` guard |
   | `lib/telegram-link.ts` | `:128-141` | `await callBff(path, postJson({}))` — then the existing `isTelegramGroupLink` guard |
   | `lib/category-mix.ts` | `:122-137` | `await callBff(\`/api/workspaces/${workspaceId}/category-mix\`, postJson({ rows }, "PUT"))` |

   Worked example, `lib/category-mix.ts:118-139` — before:

   ```ts
   export async function saveCategoryMix(
     workspaceId: string,
     rows: MixWrite[],
   ): Promise<SaveMixResult> {
     let response: Response;
     try {
       response = await fetch(`/api/workspaces/${workspaceId}/category-mix`, {
         method: "PUT",
         headers: { "Content-Type": "application/json" },
         body: JSON.stringify({ rows }),
       });
     } catch {
       return { ok: false, error: "unreachable", status: 0 };
     }
     const data = await response.json().catch(() => ({}));
     if (!response.ok) {
       const error =
         typeof data?.error === "string" ? data.error : `http_${response.status}`;
       return { ok: false, error, status: response.status };
     }
     return { ok: true, rows: Array.isArray(data?.rows) ? data.rows : [] };
   }
   ```

   After:

   ```ts
   export async function saveCategoryMix(
     workspaceId: string,
     rows: MixWrite[],
   ): Promise<SaveMixResult> {
     const result = await callBff(
       `/api/workspaces/${workspaceId}/category-mix`,
       postJson({ rows }, "PUT"),
     );
     if (!result.ok) {
       return { ok: false, error: result.error, status: result.status };
     }
     return { ok: true, rows: Array.isArray(result.data.rows) ? result.data.rows : [] };
   }
   ```

   > **`data?.x` becomes `result.data.x`.** `callBff` guarantees `data` is an
   > object, so the optional chain is no longer needed — but the *value* at each
   > key is still `unknown`, so every `typeof`/`Array.isArray` guard that
   > follows stays exactly as it is. Do not remove one.

   Importer grep after all seven:

   ```bash
   grep -rn "catch {" landing/src/lib
   ```

   Expected **before**: 9 `catch {` returning `unreachable`.
   Expected **after**: 1 — the one inside `bff.ts`. (`session.ts` and others
   have unrelated `catch` blocks; count only the `unreachable`/`status: 0` ones:
   `grep -rn 'status: 0' landing/src` → **before** 9, **after** 1.)

   Run the affected tests:

   ```bash
   npx --prefix landing vitest run src/lib/tokens.test.ts src/lib/drive.test.ts \
     src/lib/telegram-link.test.ts src/lib/category-mix.test.ts src/lib/destination.test.ts
   ```

   Expected: all green with no edits. These tests stub `fetch` and assert the
   result shape, which is exactly what did not change.

6. **Migrate the four component-local fetches.** Three are plain; the queue is
   the one with a trap.

   6a–6c. `create-workspace-form.tsx:53-66`, `accept-invitation.tsx:24-33`,
   `workspace-list.tsx:43-50` — each becomes a `callBff` call keeping its own
   error copy. Worked example, `accept-invitation.tsx`:

   Before:

   ```ts
   const response = await fetch(`/api/invitations/${token}/accept`, {
     method: "POST",
     headers: { "Content-Type": "application/json" },
   });
   const body = await response.json().catch(() => ({}));
   if (!response.ok) {
     setError(acceptRefusalCopy(body?.error, response.status));
     return;
   }
   ```

   After:

   ```ts
   const result = await callBff(`/api/invitations/${token}/accept`, postJson({}));
   if (!result.ok) {
     setError(acceptRefusalCopy(result.error, result.status));
     return;
   }
   ```

   > **Read each file's exact lines before editing** — the three differ in what
   > they do after `ok` (a redirect, a `router.refresh()`, a state set) and in
   > whether they have their own `try/catch` around the whole thing. The
   > `try/catch` goes; `callBff` never throws. What each does on failure stays
   > byte-identical, including the refusal-copy call and its arguments.
   >
   > `create-workspace-form.tsx` has an extra constraint pinned by a test:
   > `create-workspace-form.test.ts:52-70` reads this file's **source** and
   > asserts that the outage report is followed by a `return;` within 220
   > characters and that the select happens *after* the catch block. Keep the
   > early `return` immediately after `setError(...)`/`setPending(false)`, and
   > keep the select below it. Run that test after the edit:
   >
   > ```bash
   > npx --prefix landing vitest run src/components/workspace/create-workspace-form.test.ts
   > ```
   >
   > If the "does not perform the select inside the block that reports an
   > outage" case fails because the `catch` it greps for is gone, **update the
   > test to grep for the new failure block** (`if (!result.ok)`) rather than
   > weakening the assertion, and say so in the diff.

   6d. **`queue-list.tsx:124-158` — the minimal migration, and why it is
   minimal.**

   `queue-list.tsx:131` re-literals `/api/workspaces/${workspaceId}/commands/${command}`
   instead of calling `submitCommand`. Routing it through `submitCommand`
   unchanged would be a **behaviour change**: `submitCommand` treats
   `outcome === "replayed"` as a failure (`REPLAYED_ERROR`), and a
   double-clicked Approve legitimately replays and is treated as success today.
   That turns a harmless double-click into an error banner. It would also start
   sending `submission_id`, which `intentCommand` does not read — the identity
   of an intent command is the `intent_id` — so the body would grow a field the
   route ignores.

   So: **use `callBff` and the shared path builder, and nothing else.**

   Before:

   ```ts
   try {
     const response = await fetch(
       `/api/workspaces/${workspaceId}/commands/${command}`,
       {
         method: "POST",
         headers: { "Content-Type": "application/json" },
         body: JSON.stringify(body),
       },
     );

     if (!response.ok) {
       const body = await response.json().catch(() => ({}));
       setNotice({ intentId: intent.id, text: refusalCopy(body?.error) });
       // A refusal about the ROW (already moved on, no longer here) means the
       // list is stale; a refusal about the request or the session does not.
       if (response.status === 409 || response.status === 404)
         router.refresh();
       return;
     }

     router.refresh();
   } catch {
     setNotice({
       intentId: intent.id,
       text: refusalCopy("target_router_unreachable"),
     });
   } finally {
     setPending(null);
   }
   ```

   After:

   ```ts
   // `callBff` and `commandPath`, NOT `submitCommand` (#NNNN). The queue's
   // commands are intent-keyed, so they carry no `submission_id`, and a
   // double-clicked Approve REPLAYS — which `submitCommand` reports as a
   // failure and this screen has always treated as success. Routing this
   // through it unchanged would turn a double tap into an error banner.
   // Sharing the path spelling and the wire shape is the part that was pure
   // duplication; the replay rule is a real difference. Folding the two is a
   // follow-up with its own decision, not a cleanup.
   const result = await callBff(commandPath(workspaceId, command), postJson(body));

   if (!result.ok) {
     setNotice({ intentId: intent.id, text: refusalCopy(result.error) });
     // A refusal about the ROW (already moved on, no longer here) means the
     // list is stale; a refusal about the request or the session does not.
     // `unreachable` carries status 0, so it correctly refreshes nothing.
     if (result.status === 409 || result.status === 404) router.refresh();
     setPending(null);
     return;
   }

   router.refresh();
   setPending(null);
   ```

   > Keep the `finally { setPending(null); }` form if you prefer — wrap the
   > block in `try { … } finally { setPending(null); }` with no `catch`. Either
   > is correct; `callBff` does not throw. Do not leave a bare `catch` that can
   > never fire.

   **One supporting edit makes this exact.** Today the network-failure path
   calls `refusalCopy("target_router_unreachable")`; `callBff` answers
   `"unreachable"`. Add that spelling to `refusalCopy` as a second `case` on the
   existing arm — `command-client.ts:194-196` already pairs them the same way,
   so this makes the two tables agree rather than inventing anything:

   `landing/src/lib/intents.ts`, inside `refusalCopy`:

   Before:

   ```ts
       case "target_router_unreachable":
         return "Storydump cannot reach the queue right now. Nothing changed — try again shortly.";
   ```

   After:

   ```ts
       // Two spellings, one cause. `target_router_unreachable` is what the
       // server-side `workspaceFetch` reports; `unreachable` is what `callBff`
       // reports when the browser's own `fetch` threw. `settingsRefusalCopy`
       // has paired them since it was written; this table now does too.
       case "unreachable":
       case "target_router_unreachable":
         return "Storydump cannot reach the queue right now. Nothing changed — try again shortly.";
   ```

   **Pin it.** Add to `landing/src/lib/intents.test.ts`, inside the existing
   `refusalCopy` describe (or a new one if there is none):

   ```ts
   it("answers the browser's `unreachable` the same as the server's", () => {
     expect(refusalCopy("unreachable")).toBe(refusalCopy("target_router_unreachable"));
     expect(refusalCopy("unreachable")).toContain("Nothing changed");
   });
   ```

   Importer greps for step 6:

   ```bash
   grep -rn "commands/\${command}\|commands/\`" landing/src
   ```

   Expected **before**: 2 hits (`command-client.ts:71`, `queue-list.tsx:131`).
   Expected **after**: 1 hit (`command-client.ts`, inside `commandPath`).

   ```bash
   grep -rn "commandPath" landing/src
   ```

   Expected **after**: 3 hits — the declaration, `submitCommand`'s use,
   `queue-list.tsx`'s use.

   ```bash
   grep -rn "await fetch(" landing/src
   ```

   Expected **before**: 13 hits across `lib/` and components.
   Expected **after**: 1 hit — inside `bff.ts`. (`lib/telegram.ts`'s waitlist
   ping and any server-side `targetFetch` internals use their own `fetch`;
   count them and record the exact number in the PR body rather than asserting
   1 blind.)

---

#### TD-D6 — one preamble for the routes and the pages

7. **Create `landing/src/lib/route-guards.ts`.**

   ```ts
   import { NextRequest, NextResponse } from "next/server";
   import { getSessionToken, isWorkspaceId } from "./session";

   /**
    * The four things every route handler on this tier does before it does
    * anything: prove there is a session, prove the workspace segment is an id,
    * read a JSON body, and relay a refusal the API already made.
    *
    * NINETEEN COPIES (eighteen after #NNNN deleted the BFF proxy). The
    * duplication was not the problem; the VOCABULARY was. `unauthenticated`,
    * `invalid_workspace` and `malformed_body` are strings the browser switches
    * on — `refusalCopy`, `settingsRefusalCopy`, `createWorkspaceRefusalCopy`,
    * `destinationConnectRefusalCopy` and `driveRefusalCopy` all have a `case`
    * for one or more of them — and one route answering `unauthorised` or
    * `invalid_workspace_id` would miss every branch and fall to a generic
    * sentence that names no remedy. Nothing about the strings or the statuses
    * changes here; they move to where they cannot be mistyped.
    *
    * EACH GUARD RETURNS ITS VALUE OR A RESPONSE, and the caller narrows with
    * `instanceof NextResponse`. Deliberately not a thrown error: a route
    * handler that throws is a 500 in Next's own words, and "not signed in" is
    * an answer, not a fault.
    */

   /** The session token, or the 401 every route answers without one. */
   export async function requireSessionToken(): Promise<string | NextResponse> {
     const token = await getSessionToken();
     if (!token) {
       return NextResponse.json({ error: "unauthenticated" }, { status: 401 });
     }
     return token;
   }

   /** The session token AND the workspace id, or the 401/400 that stops the route. */
   export async function requireWorkspace(
     context: { params: Promise<{ id: string }> },
   ): Promise<{ token: string; id: string } | NextResponse> {
     const token = await requireSessionToken();
     if (token instanceof NextResponse) return token;
     const { id } = await context.params;
     if (!isWorkspaceId(id)) {
       return NextResponse.json({ error: "invalid_workspace" }, { status: 400 });
     }
     return { token, id };
   }

   /**
    * Relay a refusal the API already made, unchanged.
    *
    * The API's reason and status ride through verbatim — a 409
    * `illegal_transition` is a normal answer this tier has no opinion about,
    * and re-coding it here is how the two tiers come to disagree about what
    * happened.
    */
   export function passThrough(result: { error: string; status: number }): NextResponse {
     return NextResponse.json({ error: result.error }, { status: result.status });
   }

   /** The request's JSON body, or the 400 for a body that is not JSON. */
   export async function readJsonBody(
     request: NextRequest,
   ): Promise<{ raw: unknown } | NextResponse> {
     try {
       return { raw: await request.json() };
     } catch {
       return NextResponse.json({ error: "malformed_body" }, { status: 400 });
     }
   }
   ```

   **New pin** — `landing/src/lib/route-guards.test.ts`. The vocabulary is the
   thing worth pinning, and it is pure:

   ```ts
   import { describe, expect, it, vi } from "vitest";
   import { NextResponse } from "next/server";
   import { passThrough } from "./route-guards";

   /**
    * THE WORDS ARE THE CONTRACT. Every sentence the browser shows for a refusal
    * is chosen by a `switch` on one of these strings; a route that answers a
    * synonym falls through to "That did not work", which names no remedy. This
    * file exists so that a rename shows up as a failing test rather than as a
    * vaguer banner nobody files a bug about.
    */
   describe("passThrough relays the API's own refusal", () => {
     it("keeps the reason and the status exactly", async () => {
       const res = passThrough({ error: "illegal_transition", status: 409 });
       expect(res.status).toBe(409);
       expect(await res.json()).toEqual({ error: "illegal_transition" });
     });
   });
   ```

   Plus source-reading cases for the two vocabularies, in the same file:

   ```ts
   import { readFileSync } from "fs";
   import path from "path";
   import { fileURLToPath } from "url";

   const GUARDS = path.resolve(
     path.dirname(fileURLToPath(import.meta.url)),
     "./route-guards.ts",
   );

   describe("the guard vocabulary", () => {
     it("spells the three refusals the browser switches on", () => {
       // AN UNREADABLE SOURCE IS A FAILURE, NEVER A SKIP — the
       // `intent-states-contract` rule.
       let src: string;
       try {
         src = readFileSync(GUARDS, "utf8");
       } catch (err) {
         throw new Error(`cannot read ${GUARDS}: ${err}`);
       }
       expect(src).toContain('{ error: "unauthenticated" }, { status: 401 }');
       expect(src).toContain('{ error: "invalid_workspace" }, { status: 400 }');
       expect(src).toContain('{ error: "malformed_body" }, { status: 400 }');
     });
   });
   ```

   > If `next/server` cannot be imported under `environment: "node"`, keep only
   > the source-reading cases and drop the `passThrough` one. Check first —
   > `api/workspaces/route.test.ts` already exercises a route handler, so the
   > import works; copy whatever mocking that file does.

8. **Migrate the routes.** Eighteen files. Work through them in the order
   below; each is the same three or four substitutions.

   Canonical before, `app/api/workspaces/[id]/commands/[command]/route.ts:31-66`:

   ```ts
   const token = await getSessionToken();
   if (!token) return NextResponse.json({ error: "unauthenticated" }, { status: 401 });

   const { id, command } = await context.params;
   if (!isWorkspaceId(id)) {
     return NextResponse.json({ error: "invalid_workspace" }, { status: 400 });
   }
   if (!isOfferedCommand(command)) {
     return NextResponse.json({ error: "unknown_command" }, { status: 404 });
   }

   let raw: unknown;
   try {
     raw = await request.json();
   } catch {
     return NextResponse.json({ error: "malformed_body" }, { status: 400 });
   }
   ```

   After:

   ```ts
   const guard = await requireWorkspace(context);
   if (guard instanceof NextResponse) return guard;
   const { token, id } = guard;

   const { command } = await context.params;
   if (!isOfferedCommand(command)) {
     return NextResponse.json({ error: "unknown_command" }, { status: 404 });
   }

   const parsedBody = await readJsonBody(request);
   if (parsedBody instanceof NextResponse) return parsedBody;
   const raw = parsedBody.raw;
   ```

   and, at the bottom:

   ```ts
   if (!result.ok) return passThrough(result);
   ```

   > `unknown_command` stays inline. It is this route's own vocabulary, not the
   > shared preamble, and it is the only route that has it.
   >
   > This route's `context.params` is `{ id: string; command: string }`, so
   > `requireWorkspace(context)` typechecks against the narrower
   > `{ params: Promise<{ id: string }> }` by structural subtyping and
   > `await context.params` is called twice — cheap, because a resolved promise
   > is awaited twice, not resolved twice. If you prefer one await, destructure
   > `command` from a single `await context.params` before calling the guard and
   > pass `{ params: Promise.resolve({ id }) }` — **don't**; the double await is
   > simpler and the plan chooses it.

   The eighteen files, each with the guards it uses:

   | Route | Guards |
   |---|---|
   | `api/invitations/[token]/accept/route.ts` | `requireSessionToken`, `passThrough` |
   | `api/me/telegram/link/route.ts` | `requireSessionToken`, `passThrough` |
   | `api/me/tokens/route.ts` | `requireSessionToken`, `readJsonBody`, `passThrough` |
   | `api/me/tokens/[tokenId]/route.ts` | `requireSessionToken`, `passThrough` |
   | `api/workspaces/route.ts` | `requireSessionToken`, `readJsonBody`, `passThrough` |
   | `api/workspaces/[id]/select/route.ts` | `requireWorkspace`, `passThrough` |
   | `api/workspaces/[id]/sources/route.ts` | `requireWorkspace`, `readJsonBody`, `passThrough` |
   | `api/workspaces/[id]/sources/[sourceId]/route.ts` | `requireWorkspace`, `passThrough` |
   | `api/workspaces/[id]/drive/route.ts` | `requireWorkspace`, `passThrough` |
   | `api/workspaces/[id]/drive/connect/route.ts` | `requireWorkspace`, `passThrough` |
   | `api/workspaces/[id]/drive/folders/route.ts` | `requireWorkspace`, `passThrough` |
   | `api/workspaces/[id]/accounts/connect/route.ts` | `requireWorkspace`, `passThrough` |
   | `api/workspaces/[id]/accounts/[accountId]/connect/route.ts` | `requireWorkspace`, `passThrough` |
   | `api/workspaces/[id]/category-mix/route.ts` | `requireWorkspace`, `readJsonBody`, `passThrough` |
   | `api/workspaces/[id]/telegram/bind-link/route.ts` | `requireWorkspace`, `passThrough` |
   | `api/workspaces/[id]/tokens/route.ts` | `requireWorkspace`, `readJsonBody`, `passThrough` |
   | `api/workspaces/[id]/tokens/[tokenId]/route.ts` | `requireWorkspace`, `passThrough` |
   | `api/workspaces/[id]/commands/[command]/route.ts` | `requireWorkspace`, `readJsonBody`, `passThrough` |

   > `api/dashboard/[...path]/route.ts` was the nineteenth. Doc 14 deleted it.
   > If it is still present, **doc 14 has not merged** — stop and rebase.
   >
   > `app/join/[token]/start/route.ts` is **not** in this list: it has no
   > session preamble (it is the pre-sign-in handoff). It is touched in step 13
   > for `INVITE_COOKIE` only.
   >
   > The routes that check a *second* id — `isUuid(accountId)`, `isUuid(sourceId)`,
   > `isUuid(tokenId)` — keep that check inline with its own error string
   > (`invalid_account`, `invalid_source`, `invalid_token`, whatever each
   > currently answers). **Read each one and keep its exact string and status.**
   > Four routes, four different second ids; there is no rule of three here.

   Importer greps after all eighteen:

   ```bash
   grep -rn 'error: "unauthenticated" }, { status: 401' landing/src
   ```

   Expected **before**: 18 hits in route files.
   Expected **after**: 1 hit — inside `route-guards.ts`.

   ```bash
   grep -rn 'error: "invalid_workspace" }, { status: 400' landing/src
   ```

   Expected **before**: 13 hits. Expected **after**: 1 (`route-guards.ts`).

   ```bash
   grep -rn 'error: "malformed_body" }, { status: 400' landing/src
   ```

   Expected **before**: 6 hits. Expected **after**: 1 (`route-guards.ts`).

   ```bash
   grep -rn 'error: result.error }, { status: result.status' landing/src
   ```

   Expected **before**: 16 hits. Expected **after**: 1 (`route-guards.ts`).

   ```bash
   grep -rn "getSessionToken" landing/src
   ```

   Expected **after**: `session.ts` (the declaration), `route-guards.ts` (the
   one caller), and the two route tests that mock it. **If a route still calls
   it directly, that route was missed.**

   Tests to run — the four routes that have them:

   ```bash
   npx --prefix landing vitest run \
     "src/app/api/workspaces/route.test.ts" \
     "src/app/api/workspaces/[id]/tokens/route.test.ts" \
     "src/app/api/me/tokens/route.test.ts" \
     "src/app/api/auth/logout/route.test.ts"
   ```

   > `api/workspaces/[id]/tokens/route.test.ts:30-31` and
   > `api/me/tokens/route.test.ts:32-33` `vi.mock` `@/lib/session`, stubbing
   > `isUuid`/`isWorkspaceId`. After this step those routes reach `session.ts`
   > through `route-guards.ts`, which imports the same module — so the mock
   > still applies. **Run these two first**; if either fails, the mock needs the
   > same factory applied to `@/lib/route-guards`, which is a test-file edit,
   > not a guard redesign.

9. **Create `landing/src/lib/page-guards.ts` and migrate the five pages.**

   ```ts
   import { redirect } from "next/navigation";
   import { getSession, type SessionUser } from "./session";

   /**
    * Every dashboard page's first four lines, and why they exist at all.
    *
    * Middleware already required a selected workspace to reach any route under
    * `/dashboard`. This is repeated because a page is reachable in tests and in
    * a direct render without it, and `activeWorkspaceId!` would be a non-null
    * assertion on a value that is legitimately null for every brand-new user.
    *
    * Five pages carried this, four of them with that paragraph pasted verbatim
    * — which is the tell: a comment worth writing once was written four times
    * and could have been edited in one of them.
    *
    * `getSession` is `cache()`d, so calling it here costs no extra JWT
    * verification even though the layout has already called it; and
    * `redirect()` throws, so the return type has no null arm.
    */
   export async function requireWorkspacePage(): Promise<{
     session: SessionUser;
     workspaceId: string;
   }> {
     const session = await getSession().catch(() => null);
     if (!session) redirect("/login");
     const workspaceId = session.activeWorkspaceId;
     if (!workspaceId) redirect("/welcome");
     return { session, workspaceId };
   }
   ```

   Then, in each of `dashboard/page.tsx:19-26`, `queue/page.tsx:28-35`,
   `media/page.tsx:23-30`, `media/calendar/page.tsx:37-40` and
   `settings/page.tsx:88-91`:

   Before (the `dashboard/page.tsx` form; the others differ only in whitespace
   and in whether the comment is present):

   ```ts
   // Deduped with layout via React cache() — no extra JWT verification
   const session = await getSession().catch(() => null);
   if (!session) redirect("/login");
   // Middleware already required a selected workspace to reach any route under
   // /dashboard. Repeated because a page is reachable in tests and in a direct
   // render without it, and `activeWorkspaceId!` would be a non-null assertion
   // on a value that is legitimately null for every brand-new user.
   const workspaceId = session.activeWorkspaceId;
   if (!workspaceId) redirect("/welcome");
   ```

   After:

   ```ts
   const { workspaceId } = await requireWorkspacePage();
   ```

   — or `const { session, workspaceId } = await requireWorkspacePage();` in the
   two pages that use `session` afterwards (`dashboard/page.tsx` does not;
   `settings/page.tsx` reads `session.userId` and `session.workspaces`).

   > `settings/page.tsx` reads `searchParams` **before** the session
   > (`:87`, the `connected` banner). Keep that order: `const { connected } =
   > await searchParams;` stays first.
   >
   > Delete the now-unused `getSession` and `redirect` imports from each page.
   > `redirect` is still used elsewhere in some of them — check per file.

   Importer greps:

   ```bash
   grep -rn 'getSession().catch(() => null)' landing/src
   ```

   Expected **before**: 5 hits in pages, plus any in `welcome/page.tsx` /
   `workspaces/page.tsx` (those are **not** dashboard pages — they have no
   `activeWorkspaceId` requirement; leave them).
   Expected **after**: 1 hit, inside `page-guards.ts`, plus the non-dashboard
   pages untouched. Record the exact non-dashboard count before and after.

   ```bash
   grep -rn 'redirect("/welcome")' landing/src
   ```

   Expected **before**: 5 hits. Expected **after**: 1 (`page-guards.ts`).

---

#### TD-D9 — one notice banner

10. **Create `landing/src/components/ui/notice.tsx`.**

    ```tsx
    import { cn } from "@/lib/utils";

    /**
     * The banner this app says things in, because it has no toast.
     *
     * SIXTEEN HAND-WRITTEN COPIES (#NNNN), and the copies disagreed about the
     * thing that matters least visually and most to a screen reader: four of
     * the nine error boxes carried `role="alert"` and five did not, so the same
     * kind of failure was announced on some screens and silent on others. Two
     * green boxes used `text-green-900` where five used `text-green-800`.
     *
     * ROLE FOLLOWS TONE, not the author's memory. An error interrupts —
     * `role="alert"` is an assertive live region, which is right for "that did
     * not save". A success or an informational note is `role="status"`, a
     * polite one, which is right for "saved" and wrong for an interruption.
     * `setup/callout.tsx` is the marketing side's equivalent; this is the
     * dashboard's, and they stay separate because the marketing one carries
     * icons and a dark-mode palette that these banners never had.
     */
    export type NoticeTone = "error" | "success" | "info";

    export const NOTICE_CLASS: Record<NoticeTone, string> = {
      error: "rounded-md border border-red-200 bg-red-50 p-3 text-sm text-red-800",
      success:
        "rounded-md border border-green-200 bg-green-50 p-3 text-sm text-green-800",
      info: "rounded-md border bg-muted/40 p-3 text-sm",
    };

    /** Assertive for a failure, polite for everything else. */
    export function noticeRole(tone: NoticeTone): "alert" | "status" {
      return tone === "error" ? "alert" : "status";
    }

    export function Notice({
      tone = "info",
      onDismiss,
      className,
      children,
    }: {
      tone?: NoticeTone;
      /** Renders a Dismiss control. Only the Accounts tab has ever had one. */
      onDismiss?: () => void;
      className?: string;
      children: React.ReactNode;
    }) {
      return (
        <div role={noticeRole(tone)} className={cn(NOTICE_CLASS[tone], className)}>
          {onDismiss ? (
            <div className="flex items-start justify-between gap-3">
              <div className="min-w-0">{children}</div>
              <button
                type="button"
                onClick={onDismiss}
                className="shrink-0 text-xs underline underline-offset-2"
              >
                Dismiss
              </button>
            </div>
          ) : (
            children
          )}
        </div>
      );
    }
    ```

    > **No `"use client"`.** It has no state and no effects, so it renders in
    > both a server component (`settings/page.tsx`) and a client one. The
    > `onDismiss` callback is supplied by a client component, which is legal —
    > the `Notice` element is constructed there.
    >
    > **Read `accounts-tab.tsx:142` before writing the dismiss markup** and copy
    > its existing button's classes and label exactly; the snippet above is a
    > placeholder for them.

11. **Replace the sixteen banners.** Each site becomes
    `<Notice tone="…">{…}</Notice>`, keeping `className` for any layout-only
    extras the original carried (`mb-4`, `mt-4`).

    | File:line | Tone | Note |
    |---|---|---|
    | `accounts-tab.tsx:142` | `error` | has a Dismiss → pass `onDismiss`; **gains `role="alert"`** |
    | `api-tokens-tab.tsx:515` | `error` | `className="mb-4"`; **gains `role="alert"`** |
    | `api-tokens-tab.tsx:520` | `info` | `className="mb-4"` |
    | `api-tokens-tab.tsx:159` | `success` | |
    | `category-weights-card.tsx:126` | `error` | **gains `role="alert"`** |
    | `category-weights-card.tsx:131` | `success` | |
    | `danger-zone-card.tsx:116` | `error` | already has `role="alert"` |
    | `danger-zone-card.tsx:141` | `error` | already has `role="alert"` |
    | `danger-zone-card.tsx:176` | `error` | already has `role="alert"` |
    | `members-card.tsx:64` | `error` | **gains `role="alert"`** |
    | `integrations-tab.tsx:370` | `error` | **gains `role="alert"`** |
    | `integrations-tab.tsx:375` | `info` | |
    | `general-tab.tsx:357` | `error` | already has `role="alert"`; `className="mb-4"` |
    | `general-tab.tsx:381` | `success` | already has `role="status"` |
    | `general-tab.tsx:501` | `success` | |
    | `settings/page.tsx:219` | `success` | already `role="status"`; **`text-green-900` → `text-green-800`** |
    | `settings/page.tsx:230` | `success` | already `role="status"`; **`text-green-900` → `text-green-800`** |

    Worked example, `members-card.tsx:64` — before:

    ```tsx
    {error && (
      <div className="rounded-md border border-red-200 bg-red-50 p-3 text-sm text-red-800">
        {error}
      </div>
    )}
    ```

    After:

    ```tsx
    {error && <Notice tone="error">{error}</Notice>}
    ```

    **The two changes a reader could notice, named:**

    - **Five error boxes gain `role="alert"`.** No pixel moves. A screen reader
      that was silent on those five failures now announces them, which is the
      point. State this in the PR body.
    - **Two success banners go from `text-green-900` to `text-green-800`.** That
      is a shade, on `settings/page.tsx:219,230` only, and it makes seven green
      banners one colour. Chosen because `-800` is the majority (five sites) and
      because keeping both would mean keeping a `shade` prop, which is the
      duplication wearing a parameter. If a reviewer wants literally zero pixel
      change in this PR, leave those two as raw `<div role="status">`s and take
      them in a follow-up — but then say so, rather than adding the prop.

    Importer grep:

    ```bash
    grep -rn "border-red-200 bg-red-50\|border-green-200 bg-green-50\|bg-muted/40" landing/src
    ```

    Expected **before**: 16 hits across the eight component files and
    `settings/page.tsx`.
    Expected **after**: 3 hits, all inside `notice.tsx`'s `NOTICE_CLASS`.

    ```bash
    grep -rn "text-green-900" landing/src
    ```

    Expected **before**: 2 hits. Expected **after**: 0, unless the marketing
    `callout.tsx` uses it — **check and exclude it**; `setup/callout.tsx` is a
    separate component and is not touched by this PR.

    **New pin** — `landing/src/components/ui/notice.test.ts`:

    ```ts
    import { describe, expect, it } from "vitest";
    import { NOTICE_CLASS, noticeRole, type NoticeTone } from "./notice";

    /**
     * The defect this component exists for was an ACCESSIBILITY one, and it was
     * invisible: four of nine error boxes announced themselves and five did
     * not, and nothing on screen said which. The role is now a function of the
     * tone, so this is the whole of that rule in two cases.
     */
    describe("a notice announces itself according to its tone", () => {
      it("is assertive for an error and polite for the rest", () => {
        expect(noticeRole("error")).toBe("alert");
        expect(noticeRole("success")).toBe("status");
        expect(noticeRole("info")).toBe("status");
      });

      it("has one class string per tone, and only error is red", () => {
        for (const tone of ["error", "success", "info"] as const satisfies readonly NoticeTone[]) {
          expect(NOTICE_CLASS[tone], tone).toBeTruthy();
        }
        expect(NOTICE_CLASS.error).toContain("red");
        expect(NOTICE_CLASS.success).not.toContain("red");
        expect(NOTICE_CLASS.info).not.toContain("red");
      });
    });
    ```

    > If importing `notice.tsx` fails under `environment: "node"` because of the
    > JSX/React import, move `NoticeTone`, `NOTICE_CLASS` and `noticeRole` into
    > `landing/src/components/ui/notice-tone.ts`, have `notice.tsx` re-export
    > them, and point the test at the `.ts`. Check before assuming — the repo
    > already has `media-loading-contract.test.tsx`.

---

#### TD-D10 — the constants (and only the constants)

12. **`WORKSPACE_NAME_MAX`** — new file `landing/src/lib/workspace-name.ts`.

    > **Not `lib/workspaces.ts`, and this is not a preference.**
    > `lib/workspaces.ts:1` imports `./session`, which imports `next/headers`.
    > A client component (`create-workspace-form.tsx`, `general-tab.tsx`) that
    > imported from it would fail the build. `lib/tokens.ts` is the precedent
    > for a shared limit module: it holds `TOKEN_NAME_MAX` and `tokenNameValid`
    > and is imported by both a route handler and a client tab. Verify before
    > writing:
    >
    > ```bash
    > grep -n "^import" landing/src/lib/workspaces.ts landing/src/lib/session.ts | head
    > ```
    >
    > Expected: `workspaces.ts` imports `./session`; `session.ts` imports
    > `next/headers`.

    ```ts
    /**
     * How long a workspace name may be, in one place.
     *
     * `workspaces.name` is `VARCHAR(100) NOT NULL`, so 100 is the schema's
     * number and not a product preference. Three sites spelled it and a fourth
     * disagreed: the create form's `<input maxLength={120}>` let a person type
     * twenty characters the same form's own `tooLong` check then refused, and
     * the Settings rename input next to it already capped at 100.
     *
     * Modelled on `tokens.ts`'s `TOKEN_NAME_MAX` / `tokenNameValid`, and in its
     * own module for the same reason that one is: `lib/workspaces.ts` reaches
     * `next/headers` through `./session`, so a client component cannot import
     * from it.
     */
    export const WORKSPACE_NAME_MAX = 100;

    /** The shape check both the create route and the two forms apply. */
    export function workspaceNameValid(name: string): boolean {
      const trimmed = name.trim();
      return trimmed.length > 0 && trimmed.length <= WORKSPACE_NAME_MAX;
    }
    ```

    Call sites:

    - `app/api/workspaces/route.ts:53-57` — before:

      ```ts
      const trimmed = typeof name === "string" ? name.trim() : "";
      if (!trimmed || trimmed.length > 100) {
        return NextResponse.json({ error: "invalid_name" }, { status: 400 });
      }
      ```

      After:

      ```ts
      const trimmed = typeof name === "string" ? name.trim() : "";
      if (!workspaceNameValid(trimmed)) {
        return NextResponse.json({ error: "invalid_name" }, { status: 400 });
      }
      ```

      Keep the comment above it, amending `VARCHAR(100)` to cite the constant.

    - `components/workspace/create-workspace-form.tsx:37` —
      `const tooLong = trimmed.length > 100;` → `const tooLong = trimmed.length > WORKSPACE_NAME_MAX;`
    - `create-workspace-form.tsx:118` — `maxLength={120}` → `maxLength={WORKSPACE_NAME_MAX}`
    - `create-workspace-form.tsx:127` — `"Keep it to 100 characters or fewer."` →
      `` {`Keep it to ${WORKSPACE_NAME_MAX} characters or fewer.`} ``
    - `components/dashboard/settings/general-tab.tsx:374` — `maxLength={100}` →
      `maxLength={WORKSPACE_NAME_MAX}`

    > **The `120` → `100` change is visible and is the fix.** Today a person can
    > type 101–120 characters into the create form and then be told it is too
    > long; afterwards the field stops at 100, the same as the rename field on
    > Settings, and the same as the route's own refusal. The `tooLong` branch
    > stays — it is the guard that mirrors the route's 400 and it is still
    > reachable by paste in browsers that do not truncate. Name this in the PR
    > body; it is the one number whose behaviour was already wrong.

    Importer grep:

    ```bash
    grep -rn "maxLength=\{1[02]0\}\|length > 100\|100 characters" landing/src
    ```

    Expected **before**: 5 hits. Expected **after**: 0 (all via the constant).

13. **`LINK_TTL_SECONDS_FALLBACK`** in `landing/src/lib/telegram-link.ts`, and
    the minutes derived.

    ```ts
    /**
     * The API's `STATE_TTL_SECONDS`, mirrored — the fallback when a response
     * omits `expires_in_seconds`, never `0`.
     *
     * Three sites spelled `900` and a fourth spelled the same span as `15`
     * minutes, in a sentence a person reads ("expires after 15 minutes"). Two
     * numbers for one fact is how the sentence and the link come to disagree
     * about when the link dies.
     */
    export const LINK_TTL_SECONDS_FALLBACK = 900;

    /** The same span, as the sentence on the Integrations card says it. */
    export const LINK_TTL_MINUTES_FALLBACK = LINK_TTL_SECONDS_FALLBACK / 60;
    ```

    Call sites:

    - `app/api/workspaces/[id]/telegram/bind-link/route.ts:28` —
      `result.data?.expires_in_seconds ?? 900` → `?? LINK_TTL_SECONDS_FALLBACK`
    - `app/api/me/telegram/link/route.ts:36` — same substitution; keep the
      comment ("The API's `STATE_TTL_SECONDS`; the fallback is the same number,
      never 0") and point it at the constant.
    - `components/dashboard/settings/integrations-tab.tsx:405-407` — before:

      ```tsx
      {telegramLink?.expiresInSeconds
        ? Math.round(telegramLink.expiresInSeconds / 60)
        : 15}{" "}
      minutes.
      ```

      After:

      ```tsx
      {telegramLink?.expiresInSeconds
        ? Math.round(telegramLink.expiresInSeconds / 60)
        : LINK_TTL_MINUTES_FALLBACK}{" "}
      minutes.
      ```

    > **`join/[token]/start/route.ts:37`'s `maxAge: 900` is a DIFFERENT TTL** —
    > how long the invite cookie survives the round trip to Google, not how long
    > a Telegram link is valid. They are the same number by coincidence. Give it
    > its own name in step 14; do **not** point it at `LINK_TTL_SECONDS_FALLBACK`.

    ```bash
    grep -rn "?? 900\|: 15}\|maxAge: 900" landing/src
    ```

    Expected **before**: 4 hits. Expected **after**: 1 — the invite cookie's,
    which step 14 renames.

14. **`INVITE_COOKIE` moves to `lib/session.ts`**, beside `SESSION_COOKIE` and
    `WORKSPACE_COOKIE`, with its lifetime named.

    `landing/src/lib/session.ts`, after `WORKSPACE_COOKIE` (`:115`) and
    `WORKSPACE_MAX_AGE_SECONDS` (`:118`):

    ```ts
    /**
     * The invitation token, remembered just long enough to survive the round
     * trip to Google.
     *
     * A cookie rather than a `?next=` parameter: a `next` parameter is a
     * redirect target an attacker can set, and the fix for that is an allowlist
     * nobody maintains. A cookie holding only the token cannot name a
     * destination at all.
     *
     * Here rather than in `app/join/[token]/start/route.ts`, where it was
     * declared and from where two unrelated modules imported it. A route module
     * is a mounted endpoint, not a library; the other two cookie names this
     * tier owns live here, and this is the third.
     */
    export const INVITE_COOKIE = "storydump_invite";

    /** 15 minutes. Long enough for a sign-in, short enough that a stale invite dies. */
    export const INVITE_MAX_AGE_SECONDS = 60 * 15;
    ```

    Then:

    - `app/join/[token]/start/route.ts:5` — delete the declaration, import
      `INVITE_COOKIE` and `INVITE_MAX_AGE_SECONDS` from `@/lib/session`; `:37`
      `maxAge: 900` → `maxAge: INVITE_MAX_AGE_SECONDS`.
    - `app/welcome/page.tsx:8` — `from "@/app/join/[token]/start/route"` →
      `from "@/lib/session"`.
    - `app/api/invitations/[token]/accept/route.ts:9` — same substitution.

    ```bash
    grep -rn "INVITE_COOKIE" landing/src
    ```

    Expected **before**: 5 hits, two of them importing from a route module.
    Expected **after**: 5 hits, all pointing at `@/lib/session`.

    ```bash
    grep -rn 'from "@/app/' landing/src
    ```

    Expected **before**: 2 hits. Expected **after**: 0 — nothing imports from a
    route module any more.

15. **Named page limits.**

    `app/(dashboard)/dashboard/page.tsx` — add above the component, in the style
    of `queue/page.tsx:9-15` and `media/page.tsx:15-20`:

    ```ts
    /**
     * The overview's history strip. Ten is a glance, not a log — the full list
     * is the Queue's history and the counts come from `stats`, never from this
     * bounded read (`01` H5).
     */
    const HISTORY_LIMIT = 10;
    ```

    `:38` — `` `intents?state=${HISTORY_STATES}&limit=10` `` →
    `` `intents?state=${HISTORY_STATES}&limit=${HISTORY_LIMIT}` ``

    `app/(dashboard)/dashboard/media/calendar/page.tsx` — three constants:

    ```ts
    /**
     * The calendar's three bounded reads (`01` H5). History and the schedule
     * strip are drawn as dots on a month, so fifteen is what fits; the queue
     * lane lists rows, so it is the shorter ten. Every COUNT on this page comes
     * from `stats`, never from these lists.
     */
    const CALENDAR_HISTORY_LIMIT = 15;
    const CALENDAR_QUEUE_LIMIT = 10;
    const CALENDAR_SCHEDULE_LIMIT = 15;
    ```

    `:45`, `:49`, `:53` — substitute each inline `limit=15` / `limit=10` /
    `limit=15` for its constant.

    ```bash
    grep -rn "limit=1[05]" landing/src
    ```

    Expected **before**: 4 hits. Expected **after**: 0.

16. **`command-client.ts` uses `unreachableCopy`.** The sentence at `:196` and
    `:232` is byte-identical to `unreachableCopy("Nothing changed")` —
    `"Storydump cannot reach the server right now. Nothing changed — try again shortly."`
    Verified character for character against `refusal-copy.ts:64-66`.

    Before (both sites):

    ```ts
       case "unreachable":
       case "target_router_unreachable":
         return "Storydump cannot reach the server right now. Nothing changed — try again shortly.";
    ```

    After (both sites):

    ```ts
       case "unreachable":
       case "target_router_unreachable":
         return unreachableCopy("Nothing changed");
    ```

    `notAuthenticatedCopy` is already imported from `./refusal-copy` in this
    file; add `unreachableCopy` to that import.

    ```bash
    grep -rn "cannot reach the server right now" landing/src
    ```

    Expected **before**: 3 hits (`refusal-copy.ts:65`, `command-client.ts:196`,
    `command-client.ts:232`). Expected **after**: 1 (`refusal-copy.ts`).

    Run `command-client.test.ts` — it pins these sentences:

    ```bash
    npx --prefix landing vitest run src/lib/command-client.test.ts
    ```

    Expected: green with **no test edit**. If a case fails, the strings are not
    identical after all — stop and diff them rather than changing the test.

    > **`EMAIL_REGEX` stays duplicated** (`api/waitlist/route.ts:7`,
    > `waitlist-form.tsx:16`). Two sites is a coincidence, not a pattern, and
    > they are on opposite sides of the one server/client boundary this codebase
    > has. Rule of three.

---

#### TD-D11 — the props nothing passes

17. **`DashboardHeader.workspaceName`** — `components/dashboard/header.tsx:30-35,60`.

    Before:

    ```tsx
    export function DashboardHeader({
      user,
      workspaceName,
    }: {
      user: SessionUser;
      workspaceName?: string;
    }) {
    ```

    ```tsx
              {workspaceName || "Switch workspace"}
    ```

    After:

    ```tsx
    export function DashboardHeader({ user }: { user: SessionUser }) {
    ```

    ```tsx
              Switch workspace
    ```

    And amend the docblock at `:27-29`, which currently claims the header
    "states which workspace you are in":

    ```
     * So the header LINKS to where you change workspace. It does not name the
     * one you are in: the prop that would have (`workspaceName`) was never
     * passed by `(dashboard)/layout.tsx`, so the link has always read "Switch
     * workspace", and the docblock said otherwise (#NNNN).
     *
     * NAMING IT IS A CHANGE, NOT A FIX. The session carries the name
     * (`session.workspaces?.find(w => w.id === session.activeWorkspaceId)?.name`),
     * so passing it is two lines — and it would put a workspace name in the
     * chrome of every dashboard page, which is a design decision nobody has
     * taken. Deleting the dead prop is the behaviour-preserving half; wiring it
     * up is its own change.
    ```

    ```bash
    grep -rn "workspaceName" landing/src
    ```

    Expected **before**: hits in `header.tsx` (×2), `general-tab.tsx` (its own
    prop, **which is passed** by `settings/page.tsx:251` — leave it),
    `settings/page.tsx`.
    Expected **after**: the `header.tsx` hits gone; `general-tab.tsx` and
    `settings/page.tsx` unchanged. **Do not delete `GeneralTab`'s
    `workspaceName` — it is passed and used.**

18. **`CreateWorkspaceForm.submitLabel`** —
    `components/workspace/create-workspace-form.tsx:24-29,138`.

    Before:

    ```tsx
    export function CreateWorkspaceForm({
      submitLabel = "Create workspace",
      autoFocus = false,
    }: {
      submitLabel?: string;
      autoFocus?: boolean;
    }) {
    ```

    ```tsx
            {pending ? "Creating…" : submitLabel}
    ```

    After:

    ```tsx
    export function CreateWorkspaceForm({ autoFocus = false }: { autoFocus?: boolean }) {
    ```

    ```tsx
            {pending ? "Creating…" : "Create workspace"}
    ```

    ```bash
    grep -rn "submitLabel" landing/src
    ```

    Expected **before**: 3 hits, all in the component.
    Expected **after**: 0.

    ```bash
    grep -rn "<CreateWorkspaceForm" landing/src
    ```

    Expected **before and after**: 2 hits — `welcome/page.tsx:92`
    (`<CreateWorkspaceForm autoFocus />`) and `workspaces/page.tsx:81`
    (`<CreateWorkspaceForm />`). Neither passes `submitLabel`.

19. **`sidebar.tsx:40` `visibleItems`.**

    Before:

    ```tsx
      const pathname = usePathname();
      const visibleItems = navItems;
    ```

    ```tsx
            {visibleItems.map((item) => {
    ```

    After:

    ```tsx
      const pathname = usePathname();
    ```

    ```tsx
            {navItems.map((item) => {
    ```

    > **Do not touch the `mobile` parameter on `:38` or the `className` ternary
    > on `:42`.** That is the mobile-navigation defect and it is question 10.

    ```bash
    grep -rn "visibleItems" landing/src
    ```

    Expected **before**: 2 hits. Expected **after**: 0.

20. **`editable` on the three General cards.** It is `true` at the single call
    site (`settings/page.tsx:252`, the bare `editable` attribute), so removing
    it is behaviour-preserving: every `disabled={!editable}` becomes an enabled
    control that was already enabled, and every `{editable && …}` becomes an
    unconditional render of something already rendered.

    20a. `components/dashboard/settings/general-tab.tsx` — remove `editable`
    from the destructure (`:182`) and the prop type (`:192`), then:

    | Line | Before | After |
    |---|---|---|
    | `:414` | `disabled={!editable}` | *(attribute removed)* |
    | `:424` | `<Select value={hoursStart} onValueChange={setHoursStart} disabled={!editable}>` | `<Select value={hoursStart} onValueChange={setHoursStart}>` |
    | `:443` | `<Select value={hoursEnd} onValueChange={setHoursEnd} disabled={!editable}>` | `<Select value={hoursEnd} onValueChange={setHoursEnd}>` |
    | `:459` | `<Select value={tz} onValueChange={setTz} disabled={!editable}>` | `<Select value={tz} onValueChange={setTz}>` |
    | `:496` | `{editable && (` … `)}` | unwrap the children |
    | `:519` | `editable={editable}` on `<CaptionStyleCard>` | *(prop removed)* |
    | `:550` | `editable={editable}` on `<RepostCadenceCard>` | *(prop removed)* |
    | `:591` | `{editable && !wired && row.inertReason && (` | `{!wired && row.inertReason && (` |
    | `:598` | `disabled={!editable \|\| !wired \|\| togglingKey !== null}` | `disabled={!wired \|\| togglingKey !== null}` |

    > `{categoryMix}` at `:514` is a **ReactNode passed in by the page** and
    > `CategoryWeightsCard`'s own `editable={isAdmin}` (`settings/page.tsx:264`)
    > is **genuinely variable**. Leave both alone. This step removes three
    > always-true flags, not the one real one.
    >
    > The `#1070` essay at `:523-543` argues about `editable` being true for
    > this tab. It stays — it is the reasoning for `CategoryMixCard` not being
    > rendered, which is unchanged — but amend its first sentence to say the
    > flag is gone and the argument now rests on the card being broken rather
    > than on a boolean.

    20b. `caption-style-card.tsx` — remove `editable` from the prop type
    (`:19`) and the destructure (`:38`); `:78` `disabled={!editable}` removed;
    `:94` `{editable && (` unwrapped.

    20c. `repost-cadence-card.tsx` — remove `editable` from the prop type
    (`:13`) and the destructure (`:28`); `:76` and `:97` `disabled={!editable}`
    removed; `:85` and `:106` `{editable && <Button` unwrapped.

    20d. `settings/page.tsx:252` — delete the bare `editable` attribute from
    `<GeneralTab>`, and amend the `editable`-is-per-tab essay at `:35-64`. After
    doc 14 and this step, the only surviving `editable` on the screen is
    `CategoryWeightsCard`'s admin gate, which is a *role* question and not a
    "the route does not exist yet" question. Rewrite the section heading and
    body to say that:

    ```
     * ── `editable` is GONE, and what replaced it ───────────────────────────
     *
     * It marked controls whose ROUTE did not exist yet. Every one of them now
     * either exists (the General writes are on the command client, epic P3) or
     * has been removed with the door behind it (#NNNN deleted the
     * switch-account control and its BFF proxy). A permanently-true flag is the
     * mirror image of the permanently-false one #1070 refused, and neither
     * survives a reader asking what it would take to flip it.
     *
     * The one flag left on this screen is `CategoryWeightsCard`'s
     * `editable={isAdmin}`, which is a different question entirely: a ROLE
     * floor, variable per person, answered from the session's membership.
    ```

    ```bash
    grep -rn "editable" landing/src
    ```

    Expected **before** (post-doc-14): ~20 hits.
    Expected **after**: hits only in `category-weights-card.tsx` (the prop, the
    type, `:163`, `:179`, `:209`), `settings/page.tsx:264` and the amended
    essay. **No hits in `general-tab.tsx`, `caption-style-card.tsx`,
    `repost-cadence-card.tsx` or `accounts-tab.tsx`.**

21. **`category-weights-card.tsx:43-47` — the effect goes, the `key` arrives.**

    Before:

    ```tsx
      // The server's picture, re-derived when it changes (a refresh after a
      // save, a newly connected folder); a person mid-edit before a refresh keeps
      // nothing, which is the honest outcome — the numbers on screen are the
      // server's again.
      const seed = data ? JSON.stringify(data.rows) : "";
      // eslint-disable-next-line react-hooks/exhaustive-deps
      const baseline = useMemo(() => (data ? cardRows(data) : []), [seed]);
      const [rows, setRows] = useState<CardRow[]>(baseline);
      useEffect(() => setRows(baseline), [baseline]);
    ```

    After:

    ```tsx
      // The server's picture. A person mid-edit before a refresh keeps nothing,
      // which is the honest outcome — the numbers on screen are the server's
      // again.
      //
      // THE RESYNC IS A `key`, NOT AN EFFECT (#NNNN). This was prop → `useMemo`
      // with an `eslint-disable` for a dependency array that lied → `useState`
      // → `useEffect` writing that state back: four hops to say "when the
      // server's rows change, start over". `settings/page.tsx` now keys this
      // card on those rows, so React unmounts and remounts it and the baseline
      // is simply what this mount was born with. The disable comment goes with
      // it — it was suppressing the warning that the chain was wrong.
      const baseline = data ? cardRows(data) : [];
      const [rows, setRows] = useState<CardRow[]>(baseline);
    ```

    Delete the now-unused `useMemo` and `useEffect` imports from the `react`
    import line — **check the rest of the file first**, there may be other uses.

    Call site, `app/(dashboard)/dashboard/settings/page.tsx:256-267` — before:

    ```tsx
    <CategoryWeightsCard
      workspaceId={workspaceId}
      data={
        mixResult.ok && Array.isArray(mixResult.data?.rows)
          ? mixResult.data
          : null
      }
      editable={
        isAdmin
      }
    />
    ```

    After:

    ```tsx
    <CategoryWeightsCard
      // Keyed on the server's rows so a refreshed mix REMOUNTS the card rather
      // than being written into its state by an effect. Changing this key
      // discards an in-progress edit, which is the rule the card documents:
      // after a save or a newly connected folder, the numbers on screen are the
      // server's again.
      key={mixResult.ok ? JSON.stringify(mixResult.data?.rows ?? null) : "unavailable"}
      workspaceId={workspaceId}
      data={
        mixResult.ok && Array.isArray(mixResult.data?.rows)
          ? mixResult.data
          : null
      }
      editable={isAdmin}
    />
    ```

    > A `key` on an element passed as a prop works: `GeneralTab` renders
    > `{categoryMix}` at one position, and React reconciles that position by
    > element type **and** key.

    ```bash
    grep -rn "eslint-disable" landing/src
    ```

    Expected **before**: 2 hits (`category-weights-card.tsx:44`,
    `media-grid.tsx:119`).
    Expected **after**: 1 (`media-grid.tsx:119`, the Drive thumbnail — leave it).

    Manual smoke for this one specifically: Settings › General › the category
    weights card — change a weight, click Save, confirm the card redraws with
    the server's numbers and the Save button goes clean.

---

#### TD-D12 — the docblocks, and the comments that say the opposite

22. **Move the ten orphaned docblocks.** Every one of these is the same
    accident: a symbol was deleted and its docblock was left stacked above the
    *next* symbol's own docblock. The fix is a cut-and-paste with no code
    change. Each is verified present at `0966771`.

    | # | Block | Currently above | Move to |
    |---|---|---|---|
    | 1 | `command-client.ts:115-124` — "A sentence for a settings refusal… Two disjoint vocabularies, not two copies of one decision." | `submitRenameWorkspace` (which has its own one-liner at `:125`) | directly above `export function settingsRefusalCopy` (`:201`) |
    | 2 | `commands.ts:113-120` — "The commands this tier offers. NOT the port's whole vocabulary…" | `RESOLUTIONS` (which has its own one-liner at `:121`) | directly above `export const COMMAND_SPECS` |
    | 3 | `commands.ts:181-192` — "PER-SOURCE, and the empty body this used to send could never succeed… The executor reads `source_id`…" | the `rename_workspace` row (which has its own block at `:193`) | directly above the `sync_now:` row (`:279`) |
    | 4 | `commands.ts:239-248` — "Disconnect a Drive source. Same shape as `sync_now`… Built since #1083… `storydump.app/privacy` §13" | the `disconnect_account` row (which has its own block at `:249`) | **delete** — its symbol is gone |
    | 5 | `commands.ts:256-260` — "Remove a destination (owner decision 2026-09-04): the port's `active → disabled` edge…" | the `remove_member` row (which has its own block at `:261`) | directly above the `disable_account:` row (`:272`) |
    | 6 | `refusal-copy.ts:50-57` — "Observation · outcome · hedged remedy · escalation… 'may help' rather than 'Sign in again'" | `unreachableCopy` (which has its own block at `:58`) | directly above `export function notAuthenticatedCopy` (`:68`) |
    | 7 | `general-tab.tsx:88-94` — "THE predicate for 'this switch moves'. Exported because the #1155 gate asserts against it…" | `scheduleSavedNotice` (which has its own block at `:95`) | directly above `export function isLiveToggle` (`:116`) |
    | 8 | `general-tab.tsx:253-260` — "All three schedule fields in ONE command, deliberately…" | `saveName` (which has its own block at `:261`) | directly above the schedule-save handler (find it: `grep -n "async function save" general-tab.tsx`; it is the one calling `submitSettingsChange` with the three schedule keys) |
    | 9 | `integrations-tab.tsx:282-292` — "Disconnect Google Drive — REVOKE AND PAUSE, never a delete (F5 (a))…" | `linkTelegram` (which has its own block at `:293`) | directly above `async function disconnectDrive` |
    | 10 | `auth/error/page.tsx:16-30` — "The API's closed reason vocabulary, verbatim from `src/api/routes/auth.py`… REPLACED, and the mismatch is worth recording" | `generateMetadata` (which has its own block at `:31`) | `landing/src/app/auth/error/content.ts`, above whatever now holds the five reasons. **`grep -n "reason" landing/src/app/auth/error/content.ts` first**; if that file already carries an equivalent paragraph, delete the orphan instead of duplicating it and say which you did. |

    > **Verify each move by reading the block and the two symbols around it.**
    > The tell in every case is a `*/` immediately followed by `/**` — two
    > docblocks stacked with no code between them. `grep -n -A1 '^ \*/$'` on
    > each file lists them.
    >
    > **No code moves. No symbol is renamed.** `tsc` and `eslint` cannot see
    > this step at all, which is exactly why it needs the reading.

    ```bash
    grep -rn -B0 -A1 '^\s*\*/$' landing/src/lib/commands.ts landing/src/lib/command-client.ts \
      landing/src/lib/refusal-copy.ts landing/src/components/dashboard/settings/general-tab.tsx \
      landing/src/components/dashboard/settings/integrations-tab.tsx \
      landing/src/app/auth/error/page.tsx | grep -A1 '\*/' | grep -c '/\*\*'
    ```

    Expected **before**: 10 stacked pairs. Expected **after**: 0.

23. **Rewrite the stale claims.** Each is a comment that now states the opposite
    of the code. The replacement sentence is given; use it verbatim or closer to
    the truth, not vaguer.

    23a. `lib/commands.ts:170` — `// Entity-less. Capable, deliberately unwired until P3/P4.`

    `settings_change` **is** wired: `command-client.ts:108-112`'s
    `submitSettingsChange` calls it, and `general-tab.tsx` is the caller.

    Replace with:

    ```ts
       // Entity-less: the identity is the submission, not a row. WIRED since
       // P3 — `command-client.ts`'s `submitSettingsChange` is the caller and
       // General's four switches and its schedule save are the controls.
    ```

    23b. `lib/target-api.ts:4-13` — the paragraph describing `backend.ts`, which
    no longer exists.

    ```bash
    grep -rn "backend.ts\|generateUrlToken\|/api/onboarding" landing/src
    ```

    Expected: hits only in this comment. Replace the whole "`backend.ts` is the
    legacy client… dies with them in the follow-up that ports those screens onto
    the target router" passage with:

    ```
     * THE ONLY CLIENT. `backend.ts` was the legacy one — every call
     * `/api/onboarding/*`, authenticated by `generateUrlToken(chat_id, user_id)`,
     * a credential HMAC'd with the Telegram bot token that a user who had never
     * used Telegram could not produce. That is why it could not be widened to
     * serve web sign-up, and it is gone: the screens it served are on this
     * client now, and there is no second era to translate to.
    ```

    23c. `app/(dashboard)/dashboard/settings/page.tsx:25-27` —
    `"Settings — General writes, Accounts writes ONE thing (adding a destination, #1089), Integrations does not yet (#1057/#1063)."`

    Integrations **does** write: `integrations-tab.tsx:320` calls
    `disconnect_account`, and the Drive connect/sync controls are live. Replace
    with:

    ```
     * Settings — every tab writes. General is the command client (P3), Accounts
     * adds and removes destinations (#1089, and `disable_account`), Integrations
     * connects and disconnects Drive and mints Telegram links, API tokens mints
     * and revokes.
    ```

    23d. `settings/page.tsx:43-46` — `"`remove-account` and `disconnect-gdrive` map to `disconnect_account` which is UNBUILT"`.
    It is a `COMMAND_SPECS` row (`commands.ts:249`) and it is called. This
    passage is inside the `editable` essay that step 20d rewrites — fold the
    correction into that rewrite rather than leaving a second paragraph about a
    flag that no longer exists.

    23e. `components/dashboard/settings/integrations-tab.tsx:48-53` — the
    paragraph that says "Integrations, read-only (#1063). Every action on this
    tab targets a route that does not exist… All three are wired now; nothing on
    this card is held off." — it contradicts itself inside four lines. Replace
    the heading and the first paragraph with:

    ```
    /**
     * Integrations — Google Drive and Telegram, both live.
     *
     * Every action here used to target a route that did not exist
     * (`disconnect-gdrive`, `sync-media`, `oauth-url/google-drive`). All three
     * are wired: Drive connect is the per-workspace grant (069, #1165),
     * disconnect is the `disconnect_account` command, and sync is `sync_now`.
     * Nothing on this tab is held off.
    ```

    Keep the rest of the docblock (the connection-facts paragraph) as it is — it
    is accurate.

    23f. `components/workspace/router-unavailable.tsx:14-16` — "this is the
    expected state of every workspace screen until the target router is
    mounted". The router is mounted; this is now the *unexpected* state.

    Replace those three lines with:

    ```
     * It is also not an alarm. No warning colour, no error icon, no apology
     * paragraph. It used to be the EXPECTED state of every workspace screen,
     * before the target router was mounted; it is now the rare one — a deploy
     * in flight, a pool saturated — and the styling is unchanged on purpose,
     * because a reader who meets it once should be told "ask again shortly",
     * not "something is broken".
    ```

    Check the default `detail` prop at `:20` too — `"This part of Storydump is
    being connected — check back shortly."` is the not-yet-wired sentence.
    Replace with `"Storydump could not reach this part of the app just now —
    check back shortly."` and **grep for call sites that pass their own
    `detail`**, which keep theirs:

    ```bash
    grep -rn "RouterUnavailable" landing/src
    ```

    23g. `app/welcome/page.tsx:37` — `"They now live in the workspace, on /dashboard/connections."`
    `sidebar.tsx`'s docblock says `/dashboard/connections` is not built; the
    controls are on `/dashboard/settings`.

    Replace with:

    ```
     * teaches a new user their first act here is to be refused. They live in
     * the workspace now, on /dashboard/settings — the Accounts and Integrations
     * tabs.
    ```

    ```bash
    grep -rn "dashboard/connections" landing/src
    ```

    Expected **before**: 2 hits (`welcome/page.tsx:37`, `sidebar.tsx:29`).
    Expected **after**: 1 — `sidebar.tsx:29`, which mentions it correctly as the
    screen that does not exist. **Read that one and confirm it still reads true
    after doc 14 removed the Analytics entry.**

    23h. `components/dashboard/settings/repost-cadence-card.tsx:20-23` —
    `"Null in DB = use deployment env defaults (REPOST_TTL_DAYS, SKIP_TTL_DAYS). The chat_settings row is bootstrapped with those env values, so values arrive here populated for any chat created after migration 029."`

    `chat_settings` is a legacy table and migration 029 is pre-tear-out. The
    first sentence is still true and is the one the fabricated-defaults question
    turns on, so **keep it and delete the rest**:

    ```ts
    /**
     * Per-workspace repost and skip lock TTLs.
     *
     * NULL means "no workspace value — the deployment's defaults apply"
     * (`REPOST_TTL_DAYS`, `SKIP_TTL_DAYS`), which is not the same as zero and
     * not the same as any particular number. The second half of this note used
     * to describe `chat_settings` and migration 029 — a legacy table this tier
     * no longer reads.
     */
    ```

    > **Do NOT touch `:32-37`'s `?? 30` / `?? 45`.** Making the card render
    > `<Unavailable/>` for NULL is question 11. This step corrects the comment
    > that describes the situation; the card's answer to it is the owner's.
    > Leaving an accurate comment above an inaccurate default is uncomfortable
    > and correct: it is what makes the question visible.

    23i. `lib/commands.ts` and `command-client.ts` header prose that mentions
    `postApi` — **leave both**. `general-tab.tsx:33` ("They used to be
    `postApi(…)`") and `commands.ts:10` ("speaking a second, dead dialect
    through `postApi`") are accurate history of a thing doc 14 deleted. History
    is not staleness.

---

24. **CHANGELOG** — `CHANGELOG.md`, under `## [Unreleased]`. One bullet under
    `### Changed` and one under `### Fixed`. Substitute the real PR number for
    every `#NNNN` in this document before committing.

    Under `### Changed`:

    ```markdown
    - **The web tier's repeated shapes have one home each (#NNNN).** The browser's `fetch` → `{ok, error, status}` wrapper was hand-written nine times across `lib/` and four more times raw inside components; it is `lib/bff.ts`'s `callBff` now, with each caller keeping only the part that genuinely differs — a Drive folder list that is not an array, a Telegram link that is not a `t.me` URL, an authorization URL pointing at the wrong host, each a 200 that is still a failure. The queue's command call, which re-typed the command path beside `command-client.ts`'s copy, shares the path builder and the wire shape and **not** `submitCommand`: the queue's commands are intent-keyed and a double-clicked Approve legitimately replays, which `submitCommand` reports as a failure, so folding them would have turned a double tap into an error banner. Nineteen route handlers opened with the same `getSessionToken()` → 401 block (eighteen after the BFF proxy went), thirteen repeated the workspace-id check, sixteen the refusal pass-through and six the JSON-body try/catch; five dashboard pages repeated a four-line redirect preamble, four of them with the explanatory comment pasted verbatim. They are `lib/route-guards.ts` and `lib/page-guards.ts` now — and not one error string or status changed, because `unauthenticated`, `invalid_workspace` and `malformed_body` are what the browser's five refusal tables `switch` on and a synonym would fall through to a sentence naming no remedy. The numbers that were spelled twice are named once: `WORKSPACE_NAME_MAX` (the create form's input allowed 120 characters the same form then refused, while the rename field beside it already stopped at 100), `LINK_TTL_SECONDS_FALLBACK` with its minutes derived (three sites said 900 seconds and a fourth told the reader "15 minutes"), the four inline page limits, and `INVITE_COOKIE` — which two modules imported from a mounted route handler — beside the other two cookie names this tier owns. Props no call site passed are gone (`DashboardHeader`'s `workspaceName`, which is why the header has always read "Switch workspace" despite its docblock; `CreateWorkspaceForm`'s `submitLabel`; the sidebar's `visibleItems`), as are the `editable` flags that were always `true` on General, Caption style and Repost cadence — the mirror image of the permanently-false boolean #1070 refused. The category weights card resyncs by `key` instead of prop → `useMemo` → `useState` → `useEffect`, which takes the repo's last unexplained `eslint-disable` with it. Ten docblocks that sat above the wrong symbol were moved to theirs, and the comments that had come to state the opposite of the code were rewritten against it: `settings_change` is wired, Integrations writes, `disconnect_account` is built, `backend.ts` is gone, `/dashboard/connections` is `/dashboard/settings`, and `RouterUnavailable` is the rare state now rather than the expected one.
    ```

    Under `### Fixed`:

    ```markdown
    - **Five error banners were invisible to a screen reader (#NNNN).** Nine red notice boxes across Settings carried the same classes and only four carried `role="alert"`, so the same kind of failure — a refused rename, a token that would not mint, a member who could not be removed — was announced on some screens and silent on others, with nothing on the page indicating which. All sixteen notices are one `components/ui/notice.tsx` whose role follows its tone: `role="alert"` for an error, `role="status"` for a success or an informational note. No pixel moves, except that the two success banners on the Settings page join the other five at `text-green-800` instead of `text-green-900`.
    ```

## Test Plan

- **Pre-change baseline**, on doc 14's merge commit:

  ```bash
  npm --prefix landing run test
  npx --prefix landing tsc --noEmit
  npm --prefix landing run lint
  ```

  Green means every vitest file passes with **no skipped contract test** (a skip
  is a failure in this repo's convention: `intent-states-contract`,
  `wire-contract`, `idempotency-fixture`, `session-cookie-contract`,
  `destination-badge-contract`, `signout-never-a-link-contract`,
  `vitest-config`), `tsc` silent, eslint silent, `git status` clean.

- **Targeted**, after the steps they cover:

  | Steps | Command |
  |---|---|
  | 2 | `npx --prefix landing vitest run src/lib/bff.test.ts` |
  | 3–5 | `npx --prefix landing vitest run src/lib/tokens.test.ts src/lib/command-client.test.ts src/lib/drive.test.ts src/lib/telegram-link.test.ts src/lib/category-mix.test.ts src/lib/destination.test.ts` |
  | 6 | `npx --prefix landing vitest run src/lib/intents.test.ts src/components/workspace/create-workspace-form.test.ts` |
  | 7–8 | `npx --prefix landing vitest run src/lib/route-guards.test.ts "src/app/api/workspaces/route.test.ts" "src/app/api/workspaces/[id]/tokens/route.test.ts" "src/app/api/me/tokens/route.test.ts" "src/app/api/auth/logout/route.test.ts"` |
  | 9 | `npx --prefix landing vitest run src/lib/session-guards.test.ts src/components/dashboard/settings/settings-loading-contract.test.tsx` |
  | 10–11 | `npx --prefix landing vitest run src/components/ui/notice.test.ts` |
  | 12–16 | `npx --prefix landing vitest run src/lib/command-client.test.ts src/components/workspace/create-workspace-form.test.ts` |
  | 20–21 | `npx --prefix landing vitest run src/components/dashboard/settings/general-tab-toggles.test.ts` |

- **New/updated pins:**
  - `landing/src/lib/bff.test.ts` — **new**. Six cases on `callBff`: the success
    shape, the route's own error string read verbatim, the `http_<status>`
    fallback, the refused body surviving for the one caller that reads `reason`,
    a non-JSON body, and a thrown `fetch` answering `unreachable`/0. Plus two on
    `postJson`.
  - `landing/src/lib/route-guards.test.ts` — **new**. `passThrough` relays the
    API's reason and status unchanged; a source-reading case pins the three
    refusal spellings and their statuses, and fails loudly if the file cannot be
    read.
  - `landing/src/components/ui/notice.test.ts` — **new**. Role follows tone
    (`alert` for error, `status` otherwise); one class string per tone; only
    error is red.
  - `landing/src/lib/intents.test.ts` — **updated**. One case: `refusalCopy`
    answers `"unreachable"` identically to `"target_router_unreachable"`.
  - `landing/src/components/workspace/create-workspace-form.test.ts` —
    **updated only if** its two source-reading cases (`:52-70`) grep for the
    `catch` block that step 6 removes. Re-point them at `if (!result.ok)`;
    do not weaken them.
  - `landing/src/lib/command-client.test.ts` — **expected unchanged**. If it
    fails at step 16, the two sentences were not byte-identical — diff them,
    do not edit the test.
  - `landing/src/components/dashboard/settings/general-tab-toggles.test.ts` —
    **expected unchanged**. `inertReason` is deliberately kept (see
    "Withdrawn on re-reading"); if this file needs an edit, something in step 20
    went further than the plan.

- **Post-change**, from the repo root:

  ```bash
  npm --prefix landing run test
  npx --prefix landing tsc --noEmit
  npm --prefix landing run lint
  npm --prefix landing run build
  ```

## Verification Checklist

- [ ] baseline green on the parent commit (doc 14's merge): `test`, `tsc --noEmit`, `lint`, `git status` clean
- [ ] doc 14 has merged — `landing/src/app/api/dashboard/` does not exist
- [ ] importer audit: every grep in steps 2–23 run, results as stated, counts recorded in the PR body
- [ ] `grep -rn 'status: 0' landing/src` returns 1 (inside `bff.ts`)
- [ ] `grep -rn 'error: "unauthenticated" }, { status: 401' landing/src` returns 1 (inside `route-guards.ts`); same for `invalid_workspace`/400, `malformed_body`/400, and the `result.error`/`result.status` pass-through
- [ ] `grep -rn 'redirect("/welcome")' landing/src` returns 1 (inside `page-guards.ts`)
- [ ] `grep -rn "border-red-200 bg-red-50" landing/src` returns 1 (inside `notice.tsx`)
- [ ] `grep -rn "eslint-disable" landing/src` returns 1 (`media-grid.tsx`)
- [ ] `grep -rn 'from "@/app/' landing/src` returns 0
- [ ] no error string, status code or refusal sentence changed — diff `git diff -U0 | grep -E '^[-+].*"(unauthenticated|invalid_workspace|malformed_body|invalid_name|unreachable|target_router_unreachable)"'` and confirm every `-` has a matching `+` at the same spelling
- [ ] `npm --prefix landing run test` green, with no contract test skipped
- [ ] `npx --prefix landing tsc --noEmit` clean
- [ ] `npm --prefix landing run lint` clean
- [ ] `npm --prefix landing run build` succeeds
- [ ] manual smoke — `/dashboard/queue`: Approve one intent (it moves), then double-click Approve on another and confirm the second tap still refreshes the list without an error banner; `/dashboard/settings` › General: rename the workspace, save the schedule, flip a toggle, and confirm each shows its green banner; the category weights card: change a weight, Save, and confirm it redraws with the server's numbers; `/dashboard/settings` › API tokens: mint and revoke, confirm the error and info banners still render; `/dashboard/settings` › Integrations: confirm the Telegram link sentence reads "expires after 15 minutes" and the Drive card's badge is unchanged; `/workspaces`: create a workspace and confirm the name field stops at 100 characters; the dashboard header: confirm it reads "Switch workspace" and links to `/workspaces`; a signed-out visit to `/dashboard` still lands on `/login`, and a signed-in user with no workspace still lands on `/welcome`
- [ ] `CHANGELOG.md` entry added under `## [Unreleased]`, `#NNNN` replaced with the real PR number here and in every docblock that cites it
- [ ] nothing in "What NOT To Do" happened

## What NOT To Do

- **Do NOT touch the Calendar's posting-window arithmetic (D2).**
  `media/calendar/page.tsx:110-118` computes `end - start` and guards on
  `windowHours > 0`, while `lib/schedule.ts:17-18` mirrors `fn_next_slot`
  (`059_security_definer_doors.sql:147-169`): start = end ⇒ 24 h, start > end ⇒
  wraps midnight. Sharing the helper **changes the rendered figure** on a 22→02
  or a 24-hour window — which is the fix, and it is question 9 in
  `00_TECH_DEBT.md`, not a cleanup. Step 9 and step 15 edit the preamble and the
  three `limit=` literals in that file; the `perDay`/`windowHours`/
  `intervalMinutes` block and the "Posting Rate" card are left exactly as found.
- **Do NOT fix the mobile navigation (D11's `mobile` prop).** `sidebar.tsx:38`
  takes a `mobile` prop that `(dashboard)/layout.tsx:45` and `header.tsx:52`
  both fail to pass, so the hamburger's Sheet renders an aside that is
  `hidden … md:block` — empty below `md` — and the trigger's `lg:hidden`
  disagrees with the aside's `md:block`, so between `md` and `lg` both show.
  Passing the prop **makes the drawer render**. Question 10. Step 19 edits
  `visibleItems` on line 40 and nothing else in that file; step 17 edits
  `header.tsx`'s `workspaceName` and nothing else. Do not "tidy" the
  `className` ternary or the breakpoints while you are in either file.
- **Do NOT remove the fabricated settings defaults (D10).** `?? 30` / `?? 45` at
  `repost-cadence-card.tsx:32-37` and `?? "enhanced"` at
  `caption-style-card.tsx:40` show a number the workspace is not on. Rendering
  `<Unavailable/>` instead **changes what the card says**. Question 11. Step 23h
  rewrites the comment above them and step 20c removes that card's `editable`
  prop; the `??` expressions are untouched. The same applies to `?? "UTC"` at
  `general-tab.tsx:215` and `queue/page.tsx:57,64`.
- **Do NOT delete `inertReason`** (`general-tab.tsx:119,140,591-593` and
  `isLiveToggle`). See "Withdrawn on re-reading": it is read by an exported,
  gate-pinned predicate and by two cases in `general-tab-toggles.test.ts`.
- **Do NOT route `queue-list.tsx` through `submitCommand`.** Step 6d explains
  the replay rule; the minimal migration is `callBff` + `commandPath`. Do not
  add `submission_id` to the queue's body — `intentCommand` does not read it.
- **Do NOT change any error string, reason spelling, status code or refusal
  sentence.** `unauthenticated`, `invalid_workspace`, `malformed_body`,
  `invalid_name`, `unknown_command`, `unreachable`,
  `target_router_unreachable`, `illegal_transition`, `manual_mode` and every
  copy sentence in `refusal-copy.ts`, `intents.ts`, `command-client.ts`,
  `destination.ts` and `drive.ts` are a contract between the routes and the
  browser's `switch` statements. The guards move them; they do not revise them.
  The one addition — `"unreachable"` as a second `case` on `refusalCopy`'s
  existing unreachable arm (step 6) — adds a spelling that nothing previously
  sent and returns the identical sentence.
- **Do NOT decompose `integrations-tab.tsx`.** That is doc 16. This PR gives it
  a `<Notice>` (×2), a constant and two docblock moves, and leaves its 821 lines
  where they are.
- **Do NOT change the marketing copy** at
  `app/(marketing)/setup/connect/page.tsx:101-104` or `config/faqs.ts:8-11,23-26`.
  Both describe the pre-dashboard product and both are rendered text a visitor
  reads; `00_TECH_DEBT.md` §Observations routes them to whoever owns copy.
- **Do NOT put `WORKSPACE_NAME_MAX` in `lib/workspaces.ts`.** It reaches
  `next/headers` through `./session`, and two client components need the
  constant. Step 12 explains.
- **Do NOT add a `shade` or `role` prop to `<Notice>`** to preserve the two
  `text-green-900` banners. Either unify (the plan's choice) or leave those two
  sites as raw `<div>`s — a parameter that exists to keep two copies is the
  duplication with extra steps.
- **Do NOT touch anything outside `landing/` and `CHANGELOG.md`.**

## Related

- `00_TECH_DEBT.md` — the audit; row 15 of "Remediation order and dependency
  matrix", and questions 9, 10 and 11 in "Questions", which are the three
  exclusions above.
- `14_landing-one-contract.md` — the prerequisite. It deletes the nineteenth
  route, removes `AccountsTab`'s `editable`, and takes two of D12's stale claims
  (`dashboard-payloads.ts:465-484`, `accounts-tab.tsx:37-39`) with the symbols
  they described.
- `16_landing-integrations-tab.md` — the next PR; depends on this one.
- `AGENTS.md` §"Architecture" — the UI layer rule. `lib/bff.ts` is the browser's
  door to **this tier's own routes**; `lib/target-api.ts` remains the only door
  to the API, and nothing in this PR calls the API directly.
- `.claude/rules/changelog.md` — the entry format; CI's `changelog-check` gates
  any PR touching code.
- **Follow-ups this PR deliberately does not take:** fold `queue-list`'s command
  call into `submitCommand` with an option to accept `replayed` (step 6d);
  give `workspaces.ts`, `start-grant.ts`/`start-proxy.ts` and `redirect-guard.ts`
  the unit tests the coverage note asks for; decide whether `lib/telegram.ts`
  should hold `TELEGRAM_BOT_TOKEN` in the site's environment; the marketing copy
  drift.

## Origin

`/artemis-skills:audit tech debt`, 2026-09-20, on `main` at `0966771`.
Findings TD-D5, TD-D6, TD-D9, TD-D10, TD-D11 and TD-D12 from
`tech-debt-2026-09-20/research/landing.md` (scratch, not committed). Every
`path:line` in this plan was re-read at `0966771`; four items were withdrawn on
that reading and are recorded in "Withdrawn on re-reading" rather than dropped.
The exclusions (D2, D11's mobile-nav fix, D10's fabricated defaults) are
questions 9, 10 and 11 of the audit and each needs its own ruling.
