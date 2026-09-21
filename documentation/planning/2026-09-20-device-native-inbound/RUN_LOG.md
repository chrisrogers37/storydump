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

Ratified 2026-09-20; nothing built yet.

## Implementation Plan

### Steps

- **2026-09-20 — discovery and plan.** kindle discovery (three design sections approved in turn); the two junctions weighed in plan mode with one independent scorer and ratified ([the decision](../2026-09-20-device-native-inbound-decision.md)); the spec approved; this plan ratified at the forge gate with F1–F9 locked. Owed before train two: the live probe against a real public album (phase 05, step 1) and its two fixtures. Owed before any build: one `ironclad` cycle on the epic.

- **2026-09-21 — reconciled with main after the tech-debt sprint.** The branch was rebased onto `main` at `ea788875` (PR #1334). Main had taken migrations 081 and 082 and `07` §24 and §25, so the drop's migration is now 083 with §26 and the album's 084 with §27. The sprint (#1336–#1357) moved most cited files; every `path:line` in the epic and the six phases was re-verified against the rebased tree by three independent read-only checks and corrected. Substantive changes the re-verification forced: the media-source port's operations are `list_changes` / `fetch_bytes` / `probe` (not `stream`), and no adapter implements `probe` today; the scripted Drive stub the sync gate used was deleted in #1325; the API's role floors are `principal.member_session` / `admin_session`; notifications fan out through `outbox.fanout_notification`; the Settings Integrations tab is split into `drive-card.tsx`, `drive-folder-picker.tsx` and `telegram-card.tsx`, so the album gets an `album-card.tsx`; the stale egress comment phase 05 meant to rewrite was already rewritten. Forks F1–F9 unchanged.
