---
title: "[plan] Phase 7: the link to add by hand — content schedule"
type: plan
status: draft
owner: astrid
created: 2026-09-27
tags: [storydump, content-schedule, link-url, cards, issue-1413, issue-418]
repos: storydump
---

# Phase 7 — The link to add

## Summary

Part of #1413. Size: M. Written for F10's lean (a). A member can give an item a link. Its approval
card shows the link to add by hand, and Post now is hidden for that item, because an API-published
story cannot carry a link sticker. That leaves Open Instagram and Posted myself. The link belongs
to the item, so cadence cards for that item behave the same way.

## Evidence

- The column already exists: `media_items.link_url` (`054:282`, model
  `accounts_sources_media.py:272`). Nothing reads or writes it (#418).
- The publish leg sends only `media_type` and the media URL (`instagram_graph.py:134-137`), and
  Meta's reference rules out stickers on stories published through the API.

## Implementation Plan

### Dependencies
Phase 3 (the card) and phase 5 (the verb pattern); F10 and F11 locked.

### Blocks
None.

### Steps
1. **A verb:** `set_item_link` `{media_item_id, link_url | null}`, floor `member`. It accepts
   `https://` URLs only, capped at 2,048 characters, and a `null` clears the link. It writes an
   audit row.
2. **The card:** a linked item gets the line "🔗 Link to add by hand: <url>" and no Post now
   button. Telegram cards stay plain text.
3. **The web:** a link field on the Media Library item, and the link shown on its Queue row.
4. **The CLI:** a verb to set and clear the link, added to the command lists and classified, as
   in phase 5.
5. Add a `CHANGELOG.md` entry.

## Test Plan

- The verb: it accepts an https link, refuses other schemes and overlong values, and clears on
  `null`.
- The card: a linked item has the line and no Post now, whether it came from cadence or was
  planned; an unlinked item is unchanged.
- The web renders the link as text, or through an https-only link.

## Verification Checklist

- [ ] The full suite and the landing tests pass.
- [ ] On a Neon branch, a linked planned item's card shows the link, and its buttons are Open
      Instagram, Posted myself, Skip and Reject.

## What NOT To Do

- Don't render `link_url` into an `href` without the https check.
- Don't try to attach the link through the Graph API. Stories take no link sticker there.

## As built: the web half (#1622)

- **The reads.** The API's media and queue reads return `link_url`; before this the web could not see
  an item's link. There is no schema change.
- **Media Library.** A card shows its item's link and offers Link…, a dialog that saves one
  (`set_item_link`, keyed per submission) or removes it with an explicit `null`. A blank field saves
  nothing.
- **Queue.** Every row whose item has a link shows it.
- **Rendering.** A link becomes an anchor only through `httpsHref`: https and no user name or password,
  with the URL parser refusing one without a host. It opens in a new tab with no referrer. Anything
  else, such as a value stored before the port's rule, is text.
- **Approve.** Following F10 (a), a linked story awaiting approval is not offered Approve in the web
  Queue; this is its own commit, so it can be dropped. Post again on a review card is unchanged, left
  for the decision on the card (7b).
- **Refusal copy.** It states the whole rule, because the port's `invalid_args` names no part of it.
  A `link_rule` fact from the port would let the web and the CLI name the broken part; that is not built.
- **Forks weighed.** A dialog over an inline field; Remove link over a blank save; the link on every
  row of its item; shape checks only in the browser; the API read in the same PR; no sample link in
  `/demo`.

## Context
- Source skill: forge · Area: `src/services/target/` (vocabulary, command_executors, prompts), `landing/src/`, `storydump_cli/` · Effort: M · Risk: Low · Priority: Medium
