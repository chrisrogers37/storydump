# Project Mission — Storydump

## What this project is
A hosted, multi-tenant Instagram Story scheduling and automation service. A
workspace connects an Instagram account and a Google Drive folder and sets a
schedule; from then on the worker mints a slot for each account as it falls
due, picks a media item, and asks the team for approval — a card in every
bound Telegram group and a row in the web Queue. An approval posts the story
through the Instagram API, or a person posts it by hand and taps *Posted
myself*; every state change is audited. One deployment serves many tenants,
and the operator and a tenant are different parties.

## What it's becoming
The single control center for a small team's social content: pull content from
anywhere (Drive today; Instagram history, product catalogs and UGC later),
schedule and post across platforms, track what performs, and optimise
automatically. Telegram stays the fast approval layer. The web dashboard
handles everything else — accounts, integrations, the schedule, the queue, the
media library, API tokens — and the `storydump` console gives a developer or an
agent the same surface over the API. Simple enough for a meme page, powerful
enough for a dropshipping store.

## North star
Zero-friction content automation — from source to posted to optimised, with as
few manual steps as possible.

## Core mental model

The product — the ledger, the web, the card and the console — is anchored to
this hierarchy:

```
Person (Google sign-in; optionally a linked Telegram identity; personal API tokens)
  → is a member of Workspaces (the tenants), with a role: owner, admin or member
    → each Workspace has: Instagram accounts, media sources (Drive folders),
      a schedule and settings, the queue of stories, bound Telegram groups,
      members and invitations, workspace service tokens
      → each media source contains many media items
```

**Surfaces map directly to this model:**

- **Web dashboard** = workspace picker → that workspace's Overview, Queue,
  Calendar, Media Library, Analytics and Settings (General, Accounts,
  Integrations, API tokens). Management lives here; a member joins by an
  invitation link, or by speaking in a bound group with a linked Telegram
  identity.
- **A bound Telegram group** = where the workspace's team decides: one card per
  story awaiting approval (Post now where API publishing is on · Posted myself
  · Skip · Reject, and an Open Instagram link), and a review card for a story
  whose outcome is unknown. Nothing is typed in the chat; a tap is the whole
  interface.
- **The `storydump` console and the API** = the same reads for a developer or
  an agent, under a person-bound token or a workspace service token; the
  writes only under a person-bound token minted with the operator role — a
  service token reads only, and a read-only person token never writes.

Every command, screen and endpoint knows whether it operates at the person
level (sign-in, tokens, memberships) or the workspace level (everything a
tenant owns), and the database enforces the second: every tenant table carries
a row-level-security policy keyed on the workspace, and every state change
goes through one command port with a closed vocabulary and a role floor per
command.

## Guiding principles
- **Simple over powerful.** If a feature needs explanation, simplify it.
- **Web-first for management, Telegram-first for decisions.** An approval is a
  tap on a card; everything else belongs on the web.
- **One authority.** The database decides what a story may become next; no
  Python pre-check copies that rule.
- **One command port.** A click, a tap and a console verb hand the same command
  to the same port and are audited the same way.
- **Closed loop.** Every post generates data that improves the next post.
- **Onboarding should take minutes, not hours.** Sign in with Google, connect an
  account, connect a folder, add the bot to a group.
- **Build for 2 teams today, design for 200.** Tenancy is a product
  requirement, not a hardening preference.

## In bounds for autonomous work
- Bug fixes and UX polish
- Instagram API integration improvements
- Content pipeline enhancements (new sources, better scheduling)
- Analytics and performance tracking
- Test coverage and code quality
- The card's and the console's copy and flow

## Requires approval
- New platform integrations (TikTok, Twitter, Reels)
- Shopify/product catalog integration
- Web dashboard architecture decisions
- Auth/login flow changes
- Database schema changes
- Anything that changes the tenancy model (workspaces, roles, the row-level
  policies) or the command vocabulary
- Anything that posts: an agent never approves a story

## Success metrics
- Time from content creation to posted: decreasing
- Manual steps per post: decreasing
- Repeat usage (daily active posting): increasing
- Onboarding time for a new workspace: under 5 minutes
