---
title: "Split the 821-line integrations tab into the cards its siblings already are, and give the folder picker a testable core"
type: plan
status: draft
owner: chris
created: 2026-09-20
tags: [plan, tech-debt, priority:medium, landing]
links: []
---

# 16 — The landing integrations tab

| | |
|---|---|
| **PR title** | refactor(landing): the integrations tab becomes cards, and the Drive folder picker gets a core a test can reach |
| **Risk** | Medium — no component test exists today (vitest runs in `node`, so nothing renders), so the safety net has to be built as part of the split |
| **Effort** | M (≈6h) |
| **Files modified** | `integrations-tab.tsx` → plus `drive-folder-picker.tsx`, `telegram-card.tsx`, `drive-card.tsx`; new pure-export test; `CHANGELOG.md` |
| **Findings addressed** | TD-D7 |
| **Depends on** | 15 (it lands `callBff`, `<Notice>` and the named constants this tab will use) |
| **Blocks** | nothing |

## Summary

`integrations-tab.tsx` is 821 lines holding three cards, a folder-picker dialog and nineteen
`useState` hooks — eight of them the picker's alone, five the Telegram links' — while both of its
siblings in the same directory are already composed of cards: `general-tab.tsx` delegates to
`CaptionStyleCard`, `RepostCadenceCard` and `DangerZoneCard`, and `api-tokens-tab.tsx` to
`MintDialog`, `TokenCard` and `MintedSecretBlock`. The consequence is not only reading cost: every
change to Drive re-renders and re-reads Telegram, and the picker's state cannot be tested at all.
`api-tokens-tab.tsx` also shows *how* to make this testable in a suite with no DOM — it exports
pure helpers (`tokenStateBadge`, `roleCopy`, `mintFormValid`, `secretSlot`) beside its components,
and those exports are what its test file reaches. This PR follows that model. What must not
change: every sentence shown, every request made, and the order the three cards appear in.

## Findings addressed

| ID | Where | One line |
|---|---|---|
| TD-D7 | `integrations-tab.tsx:75-142` | 19 `useState` hooks in one component — 8 picker, 5 Telegram links |
| TD-D7 | `:185-263` + `:683-807` | The folder-picker dialog: ~230 self-contained lines, six handlers, no test |
| TD-D7 | `:99-126,:300-314,:379-538` | The Telegram identity link and the group link — two near-identical blocks with paired state |
| TD-D7 | `:539-681` | The Drive card |
| TD-D7 | `:809-818` | The media count |

## Dependencies

- **Depends on 15.** That PR introduces `callBff`, the `<Notice>` component and the named
  constants (the link TTL among them) that this tab uses throughout. Splitting first would mean
  extracting components and then editing every one of them again.
- Blocks nothing. It is the last of the three `landing/` docs.

## Implementation Plan

### Steps

Each step leaves `npm --prefix landing run test`, `tsc --noEmit`, `lint` and `build` green.

1. **Baseline and inventory** — record in the PR:
   ```bash
   cd landing
   npx tsc --noEmit && npx eslint src && npm run test && npm run build
   grep -c "useState" src/components/dashboard/settings/integrations-tab.tsx     # 19
   grep -rn "IntegrationsTab" src                                                # one importer: settings/page.tsx
   ```

2. **Extract `DriveFolderPickerDialog`** — the biggest and most self-contained piece: state at
   `:132-141` (`pickerOpen`, `pickerStack`, `pickerFolders`, `pickerLoading`, `pickerError`,
   `pickingId`, `pickerRoot`, `pickerTruncated`), the handlers at `:185-263`, the `<Dialog>` at
   `:683-807`. New file `drive-folder-picker.tsx` in the same directory, with the props the
   research identified: `workspaceId`, `connectedRefs`, `open`, `onPicked`, `onClose`.

   **Export the pure parts separately**, the way `api-tokens-tab.tsx` does — this is what makes
   the picker testable in a suite with no DOM. Candidates, confirmed by reading the handlers:
   the breadcrumb/stack derivation (given a stack, what is displayed), the "already connected"
   predicate against `connectedRefs`, and the root toggle's label logic. Each becomes a named
   export with no hooks in it.

   The tab keeps `pickerOpen` only if it owns the trigger; prefer moving even that into the
   dialog and passing a trigger element, whichever reads closer to `MintDialog` at
   `api-tokens-tab.tsx:201`.

3. **Extract `TelegramCard`** — the identity link (`:410-448`) and the group link (`:490-535`)
   are near-identical blocks with paired state (`telegramLink`/`groupLink`,
   `linkingTelegram`/`mintingGroupLink`). Inside the new `telegram-card.tsx`, factor the shared
   shape into one internal `OneShotLinkBlock` taking the differing pieces as props — **read both
   blocks first and list what actually differs** (the mint call, the heading, the empty-state
   sentence, whether there is an error slot: `groupLinkError` exists, the identity link has no
   equivalent). If the differences turn out to be more than three props, keep them as two blocks
   and say so — a component with eight props is the duplication again with indirection.

4. **Extract `DriveCard`** — `:539-681`, with `syncingId`, `disconnecting`, `connecting`,
   `removingId` and the folder list. It takes the picker from step 2 as a child or renders it
   behind its own trigger.

5. **What stays in `IntegrationsTab`** — composition, the shared error/notice banner (which is
   `<Notice>` after doc 15), the media count at `:809-818`, and the three cards in their current
   order. Target: under 150 lines.

6. **Write the test that does not exist.** There is no component test for this tab, and vitest
   runs in the `node` environment (`vitest.config.ts`) so nothing can be rendered. Add
   `drive-folder-picker.test.ts` covering the pure exports from step 2 — at minimum the
   "already connected" predicate and the breadcrumb derivation, including the empty stack and a
   folder that appears twice. This is the only new safety net in this PR, so it is not optional:
   without it the split has nothing but `tsc` behind it.

7. **Verify the tab's behaviour by hand**, since no automated test renders it. On
   `/dashboard/settings` → Integrations, with the dev server (`npm --prefix landing run dev`):
   open the folder picker, navigate into a folder and back, switch My Drive / Shared, pick a
   folder, see it appear; mint a Telegram identity link and a group link; disconnect and
   reconnect Drive. Paste what you checked in the PR. **Use a development workspace** — this
   tab writes real integration state.

8. **CHANGELOG** — under `## [Unreleased]` → `### Changed`:

   > **The Integrations tab is cards, like the two tabs beside it (#1216).** It held three cards,
   > a folder-picker dialog and nineteen `useState` hooks in one 821-line component, so a change
   > to Drive re-rendered Telegram and the picker's logic could not be tested at all — while its
   > siblings `general-tab.tsx` and `api-tokens-tab.tsx` were already composed of cards. It is now
   > `DriveCard`, `TelegramCard` and `DriveFolderPickerDialog` with the tab as composition, and
   > the picker's pure logic is exported and tested the way the tokens tab's helpers already are.
   > Every sentence, request and card order is unchanged.

## Test Plan

- **Pre-change baseline** (all four, recorded in the PR):
  ```bash
  cd landing && npx tsc --noEmit && npx eslint src && npm run test && npm run build
  ```
- **The characterization problem, stated plainly:** there is no existing test of this component.
  `drive.test.ts` and `telegram-link.test.ts` cover the `lib/` half it calls, and they are the
  only automated evidence that the extracted cards still talk to the right doors. The new test
  from step 6 plus the manual pass in step 7 are the rest of the net — the PR must say so rather
  than implying the suite covers this.
- **Targeted:** `npm --prefix landing run test -- drive telegram-link drive-folder-picker`
- **New pins:** `drive-folder-picker.test.ts` — the pure exports from step 2.
- **Post-change:** the same four commands, plus step 7's manual pass.

## Verification Checklist

- [ ] 15 has landed
- [ ] baseline: `tsc --noEmit`, `eslint`, `test`, `build` all green on the parent commit
- [ ] importer audit: `grep -rn "IntegrationsTab" landing/src` — still one importer, `settings/page.tsx`
- [ ] `IntegrationsTab` is under ~150 lines and the three cards appear in the same order
- [ ] no sentence, label or request URL changed (diff-read the JSX, don't assume)
- [ ] `drive-folder-picker.test.ts` exists and covers the two named cases
- [ ] `npm --prefix landing run test` green
- [ ] `npx --prefix landing tsc --noEmit` clean
- [ ] `npm --prefix landing run lint` clean
- [ ] `npm --prefix landing run build` succeeds
- [ ] manual pass from step 7 done on a development workspace, pasted in the PR
- [ ] `CHANGELOG.md` entry under `[Unreleased]`
- [ ] nothing in "What NOT To Do" happened

## What NOT To Do

- **Do not split without adding the test** (step 6). This is the one component in the tab family
  with no automated coverage; a pure-refactor PR whose only check is the type-checker is how a
  silent regression ships.
- **Do not change a single user-visible string** while moving JSX. The empty states and the link
  sentences are what a person reads when an integration is half-configured.
- **Do not convert the picker's state to a reducer, a context or a store** on the way past. The
  brief is to move it, not to re-architect it; `MintDialog` keeps plain `useState` and is the
  model.
- **Do not fold `TelegramCard`'s two link blocks into one component taking eight props** (step 3).
- **Do not connect or disconnect a production workspace's Drive or Telegram** during step 7.
- **Do not touch `accounts-tab.tsx`'s `editable` flag or the `switch-account` path** — doc 14
  deletes both, and if it has not landed yet, leave them alone.
- **Do not add a DOM test environment to vitest** to make component rendering testable. That is a
  real proposal with real trade-offs (`vitest.config.ts` is pinned by its own contract test) and
  it is not a tech-debt cleanup — file it separately if you want it.

## Related

- `00_TECH_DEBT.md` — finding TD-D7.
- `14_landing-one-contract.md`, `15_landing-shared-shapes.md` — land first, in that order.
- `landing/src/components/dashboard/settings/api-tokens-tab.tsx` — the model this PR follows.

## Origin

`/artemis-skills:audit tech debt`, 2026-09-20, on `main` at `0966771`. Research:
`tech-debt-2026-09-20/research/landing.md`, finding TD-D7. The nineteen hooks, the picker's eight,
the Telegram pair's five and both siblings' composition patterns were re-verified while writing
this plan.
