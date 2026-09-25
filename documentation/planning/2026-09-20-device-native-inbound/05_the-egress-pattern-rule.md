---
title: "Device-native inbound — phase 05: the egress floor admits two anchored host patterns for Apple's shared-album feed (PR 5)"
type: plan
status: active
owner: chris
created: 2026-09-20
tags: [plan, security, egress]
links: []
---

> Phase 05 of [`00_EPIC.md`](00_EPIC.md), ratified 2026-09-20 at the forge gate and folded after ironclad cycle 1 (2026-09-25). **Waits on F10 (open): built only if the owner keeps the album.** Spec: [`2026-09-20-device-native-inbound-spec.md`](../2026-09-20-device-native-inbound-spec.md). The ledger is [`RUN_LOG.md`](RUN_LOG.md); its entry for this phase records where the build departs from this text.

## Summary

The floor's allow-list matches exact host names. Apple's shared-album feed answers from partitioned hosts and names its asset hosts in the response, so the album adapter cannot be reached without a rule. This phase adds `allowed_host_patterns`, a tuple of anchored regular expressions checked with `fullmatch`, two entries, and leaves every other guard (resolution, the private-address block, the pinned connection, the redirect refusal, the byte cap) exactly as it is. It ships alone because it is a change to a security control. The asset-host pattern is pinned by the live probe against a real album, which runs first and is recorded in the epic's ledger.

## Evidence

- `src/services/target/egress.py:133-141` `DEFAULT_ALLOWED_HOSTS` — five exact names. `:167-183` `EgressPolicy` (`timeout_class`, `total_budget_s`, `max_attempts`, `max_response_bytes`, `allowed_hosts`, the six `enforce_*` flags). `:271` `validate_target`; the membership line `:286-289` (`host not in policy.allowed_hosts` → `EgressRefused`); `:291-307` resolution and the per-address `_is_forbidden` check (`:205-214`); `:242-268` `_pin_call` (#871: the validated address pinned to the connection, `Host` header and SNI set); the cross-host redirect refusal `:395-402`. The comment at `:125-132` was rewritten on 2026-09-21 and already says #871 landed.
- A narrowed policy precedent: `src/services/target/transit.py:501-507` builds `EgressPolicy(allowed_hosts=frozenset({host}))` (`:506`).
- Tests: `tests/src/services/target/test_egress_floor.py:303` an unlisted host is refused, `:307` the same host with the allow-list disabled, `:209`/`:236`/`:247` the private-address classes, pinning at `:666, :691, :733, :876, :984, :1054`; `tests/src/services/target/test_egress_hosts.py:15, :20` the allow-list's membership pins.
- The feed's hosts, from the open-source client read on 2026-09-20: the base is `p<NN>-sharedstreams.icloud.com` after a 330 redirect from `p01`; asset URLs are composed from `url_location` + `url_path` in the asset response, whose host family the probe records.

## Implementation Plan

### Dependencies

F10 locked as (a). The live probe (step 1) is an external gate: it needs a real public album from the owner. No code dependency.

### Blocks

Phase 06.

### Steps

1. **The probe.** `scripts/probe_icloud_album.py`: given a public album link, resolve the partition host, call the web stream and the asset-URL endpoints, and print the album's name, item count, the derivative keys of one item, the change tag, and the distinct asset hosts, with the token redacted. Run it only after F10 keeps the album — making an album public just to test the feed is the exposure F10 decides — and then once against the owner's album; paste the output into `RUN_LOG.md`; save the two responses, token redacted, as `tests/fixtures/icloud_album/webstream.json` and `webasseturls.json` for phase 06.
2. **The policy.** `EgressPolicy` gains `allowed_host_patterns: tuple[re.Pattern[str], ...] = ()`. `DEFAULT_ALLOWED_HOST_PATTERNS = (re.compile(r"p\d{1,3}-sharedstreams\.icloud\.com"), re.compile(r"<the asset host family the probe records>"))`, the second pattern written the day the probe runs, with its look-alike tests in the same commit, defined beside `DEFAULT_ALLOWED_HOSTS` and not folded into the default policy: only the album adapter's policy names them (F6).
3. **The check.** `validate_target:286-289` becomes: allowed when `host in policy.allowed_hosts` or any pattern `fullmatch`es `host`; the refusal message names both lists. `host` is `urlsplit(...).hostname`, already lower-cased and port-free; the private-address check and pinning run after, unchanged.
4. **The comment.** `egress.py:125-132` already records that #871 landed (rewritten 2026-09-21); one sentence is added saying pattern hosts are admitted deliberately and reviewed.
5. **Docs.** The module docstring's allow-list paragraph (`:33-42`); `.claude/rules/development-patterns.md` if it names the allow-list (check at build).
6. **CHANGELOG** under Unreleased.

## Test Plan

- `test_egress_floor.py`: a pattern host is accepted under a policy naming the patterns and refused under the default policy; look-alikes refused — `p01-sharedstreams.icloud.com.evil.com`, `xp01-sharedstreams.icloud.com`, `p01-sharedstreams.icloud.co`, an upper-case spelling (accepted only because `hostname` lower-cases it, asserted explicitly); a pattern host resolving to a private address is refused; the pinned call for a pattern host sets `Host` and SNI to it. `test_egress_hosts.py`: the pattern tuple is exactly two entries and each is anchored (`pattern.pattern` has no leading `^`/`$` reliance: `fullmatch` is asserted, not the string).

## Verification Checklist

- [ ] `.venv/bin/pytest tests/src/services/target/test_egress_floor.py tests/src/services/target/test_egress_hosts.py --no-cov -q` green.
- [ ] `DEFAULT_ALLOWED_HOSTS` unchanged (`git diff`).
- [ ] `RUN_LOG.md` carries the probe's output and the two fixtures exist with no token in them (`git grep -c <token>` is 0).

## What NOT To Do

No suffix matching, no widening of the exact-host set, no disabling of resolution or the private-address block for pattern hosts, no pattern in the default policy.

## Context

Area: services (`egress`) · Effort: S · Risk: medium (a security control) · Priority: high for train two, if F10 keeps it.
