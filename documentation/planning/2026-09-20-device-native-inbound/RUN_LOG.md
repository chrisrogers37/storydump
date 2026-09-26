---
title: "Device-native inbound — the ledger (RUN_LOG)"
type: plan
status: active
owner: chris
created: 2026-09-20
tags: [plan, ledger, media-sources, telegram, icloud]
links: []
---

# RUN_LOG — device-native inbound

The ledger of [`00_EPIC.md`](00_EPIC.md): one entry per event, newest last. A phase's entry records where the build departed from its plan text.

## Summary

Ratified 2026-09-20. Ironclad cycle 1 run and folded on 2026-09-25; three owner decisions open (F10–F12); nothing built yet.

## Implementation Plan

### Steps

- **2026-09-20 — discovery and plan.** kindle discovery (three design sections approved in turn); the two junctions weighed in plan mode with one independent scorer and ratified ([the decision](../2026-09-20-device-native-inbound-decision.md)); the spec approved; this plan ratified at the forge gate with F1–F9 locked. Owed before train two: the live probe against a real public album (phase 05, step 1) and its two fixtures. Owed before any build: one `ironclad` cycle on the epic.

- **2026-09-21 — reconciled with main after the tech-debt sprint.** The branch was rebased onto `main` at `ea788875` (PR #1334). Main had taken migrations 081 and 082 and `07` §24 and §25, so the drop's migration is now 083 with §26 and the album's 084 with §27. The sprint (#1336–#1357) moved most cited files; every `path:line` in the epic and the six phases was re-verified against the rebased tree by three independent read-only checks and corrected. Substantive changes the re-verification forced: the media-source port's operations are `list_changes` / `fetch_bytes` / `probe` (not `stream`), and no adapter implements `probe` today; the scripted Drive stub the sync gate used was deleted in #1325; the API's role floors are `principal.member_session` / `admin_session`; notifications fan out through `outbox.fanout_notification`; the Settings Integrations tab is split into `drive-card.tsx`, `drive-folder-picker.tsx` and `telegram-card.tsx`, so the album gets an `album-card.tsx`; the stale egress comment phase 05 meant to rewrite was already rewritten. Forks F1–F9 unchanged.

- **2026-09-25 — refreshed against main, then ironclad cycle 1 and its fold.** `main` at `29acea2e` was merged into the branch (`ff537792`). Main had taken 083 and §26 (#1381), the second collision, so the phases now say "the next free number and section at build time"; the citations into the files main changed were re-verified by two read-only passes and corrected, and the worker's new transaction pattern (f5088231, 7561f7f7) was folded into phases 03 and 04 (`89fb62ac`). **Ironclad cycle 1:** ten lenses, none failed — 2 critical, 15 major, 13 groups of minor findings, 6 questions — [posted on #1334](https://github.com/chrisrogers37/storydump/pull/1334#issuecomment-5836147615). **Folded the same day:** the drop-folder lookup scoped to the workspace, with the ledger as the authority (the critical cross-tenant finding); a recovery path for a folder the current grant cannot write; savepoint, GUCs and a named failure outcome for the drop's admission; admission only for `joined` or `already_member`; GIFs, stickers and video notes excluded; per-kind caps at the story path's limits (8 MiB images, 20 MiB videos); `ck_rate_scope` widened for paced replies; the item recorded from the upload response; refusals written exactly once; a threaded final-failure reply; F3's attempts corrected from 5 to 12 so its locked intent holds; phase 02 split into a reader PR and a writer PR (deploy skew), with the runbook and design record amended there; phase 03 split into 03a and 03b; the admit rule keeping Off rows and flooring frozen shares; phase 04 leaving the `fn_prompts_due` door alone and making the adapter contract provider-neutral; phase 06 without the quarantine widening or a stub, with the token encrypted and refusals by name; the plan-health fixes. **Opened for the owner:** F10 (the album: lean defer), F11 (what counts as a drop and who may drop: lean an admin switch per group, off by default), F12 (how a system source enters the mix: lean a reserved share inside `weights()`). **Not converged:** the fold resolves the technical findings in the text; convergence needs F10–F12 locked and a confirming cycle 2.
