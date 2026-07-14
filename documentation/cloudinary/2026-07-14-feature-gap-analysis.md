# Cloudinary Feature Gap Analysis & Enhancement Proposals

**Status:** Proposed
**Author:** astrid
**Date:** 2026-07-14
**Related:** #317, #500, #450, #550, #184, #294, #152, #189, #515, #549, #557; PR #510 (queue/posting simplification plan)
**Trigger:** Cloudinary's July 2026 product announcements (AI image generation, self-service OAuth, VS Code extension GA, performance/media-experience improvements), evaluated against storydump's current media pipeline.

---

## 1. How storydump uses Cloudinary today

Cloudinary's role today is deliberately narrow: **a transient hop to get a public URL for Meta's Graph API**. It is not storage, not the Mini App image source, and not a transformation platform.

```
media source (/tmp upload | Google Drive | Telegram)
        │  bytes fetched at POST TIME
        ▼
cloudinary.uploader.upload()            ── sync, no timeout, on the shared event loop
        │  folder: instagram_stories/{chat_settings_id}, random public_id,
        │  no preset / tags / eager transforms / webhooks
        ▼
get_story_optimized_url()               ── images only; video passes RAW
        │  blurred-underlay letterbox chain → 1080×1920 (9:16)
        ▼
Meta Graph API STORIES container → publish
        │
        ▼
delete immediately (finally-block) + hourly cloud_cleanup_loop safety net
```

Key facts (all verified against `main` @ 7c99a34):

- **Upload**: `CloudStorageService.upload_media()` (`src/services/integrations/cloud_storage.py:65-168`) — signed server-side upload, options limited to `folder`/`resource_type`/`overwrite` (`:197-216`). No upload preset, no tags, no eager transforms, no `notification_url`, no moderation.
- **Two duplicated pipelines** (#500): the interactive autopost button (`telegram_autopost.py:201-285`) and the headless auto-approve path (`scheduler.py:641-724`) each implement download → upload → transform → post → cleanup, with diverging behavior (only the button path persists `cloud_*` DB refs; media-type detection differs subtly).
- **Transform**: one delivery-time URL chain (`cloud_storage.py:359-423`) — `u_<self>/c_fill,w_1080,h_1920,e_blur:2000/fl_layer_apply/c_limit,w_1080/c_pad,w_1080,h_1920,g_center`. **No `f_auto`/`q_auto` anywhere. Video gets no transform at all** (`telegram_autopost.py:462-463`, `scheduler.py:696-700`).
- **Lifecycle**: retention = `CLOUD_UPLOAD_RETENTION_HOURS` (24h). Immediate delete after posting; `cleanup_expired` (paginated since #499/PR #508) sweeps the folder hourly by `created_at` age. The loop sleeps **before** working (#550) and has zero tests (#450). `media_items.cloud_expires_at` exists but is **never written** — retention is derived from `cloud_uploaded_at`.
- **Storage reality**: Mini App uploads land on **ephemeral `/tmp`** (`dashboard.py:293`, issue #317); the code comment already names the intended fix: *"Planned migration: Cloudinary persistent storage (cloud_url columns exist on model)."*
- **Dedup**: exact SHA256 only (`media_item.py:37`, 409-on-duplicate at `dashboard.py:420`). No perceptual hashing.
- **Operational risk on record** (fleet knowledge, hang/perf regression doc 2026-06-12, prod-confirmed via #557): `uploader.upload()` runs **synchronously with no timeout on the single shared event loop** — a TCP stall freezes the whole bot. The documented fix direction (finite timeouts + `to_thread` offload) is additive to PR #510's Phase 4 and is a precondition for any expanded Cloudinary usage.

## 2. What Cloudinary announced (July 2026) — verified

Verification sourced from Cloudinary's release notes (dated 2026-07-09), product docs, and pricing page, fetched 2026-07-14. Confidence marked per claim; §7 lists what could not be verified.

| Email claim | What it actually is | Status | Relevance to storydump |
|---|---|---|---|
| **AI image generation** | New **Image Generation add-on**: full text-to-image REST API (`POST /v2/generate/<cloud>/text_to_image`), models incl. flux/recraft/gpt-image/nano-banana/ideogram, server-side callable (Basic Auth, Python-friendly), output storable as managed or temporary asset. Separate credit-based billing, not gated to plan tier. Corroborated; note: release notes say GA, the API doc still carries an "early version, endpoints may change" flag. | NEW | High (exploratory) — §P7 |
| **Easier OAuth** | Self-service OAuth-app creation in the Console (previously required a support ticket). OAuth tokens become an *alternative* to API key/secret for Upload/Admin APIs. **Key/secret signature auth is unchanged.** Corroborated. | NEW | **None operationally** — our server-side key/secret auth needs no change. Noted in §6. |
| **Enhanced VS Code extension** | v1.0 GA (~2026-07-09): in-IDE media library, docs AI assistant, MCP/agent-skill one-step setup. Works in VS Code forks. Corroborated. | NEW | Dev-tooling only; no product impact. §6. |
| **Time-to-production / performance / media experience** | Studio UI (new transformation builder, all plans), **Video Moderation GA**, q_auto patent announcement (existing tech), misc API conveniences. Single-source (release notes). | NEW (mixed) | Video Moderation is relevant if/when UGC broadens; Studio UI helps us author transform chains. |

**Existing capabilities the research confirmed that matter to us** (not new, but unused by storydump today): `b_gen_fill` generative aspect-ratio extension (can extend any image to 9:16 with content-aware fill — images must be non-transparent; `e_gen_background_replace` still Beta), `g_auto` smart crop for **both images and video**, `q_auto`/`f_auto` delivery optimization, upload presets (incoming + **eager** transforms, auto-tagging, moderation at upload), unsigned presets / signed direct browser uploads, video transcode/trim/thumbnail/overlays (text overlays render as images; time-scoped), `phash: true` at upload (64-bit perceptual fingerprint, images only, DIY Hamming comparison), and — importantly — **no native per-asset TTL exists**: Cloudinary's own documented pattern for temporary UGC is exactly what we built (scheduled deletion by tag/date search). Our engineered cleanup loop is the right shape; its gaps are tags, ordering (#550), and tests (#450).

## 3. Gap analysis

| Capability (Cloudinary) | Our state | Gap / opportunity | Proposal |
|---|---|---|---|
| Persistent managed storage + delivery | `/tmp` is source of truth for web uploads (#317); Cloudinary transient-only | Durable, redeploy-proof media; `cloud_url` becomes primary ref (the fix #317 already names) | **P1 (M)** |
| Tags + upload presets + eager transforms | Raw `upload()` with folder only; cleanup keys off folder+age; `cloud_expires_at` never written | Precise, tag-scoped lifecycle; server-owned upload policy; safe coexistence of persistent + transient assets | **P2 (S)** |
| `q_auto` / `f_auto` delivery optimization | Absent from the story URL chain | Smaller payloads to Meta, faster container readiness; near-zero effort | **P3 (S)** |
| `b_gen_fill` 9:16 generative extension | Blurred-underlay letterbox | Content-aware story framing as a per-tenant **toggle** (visual changes ship as toggles) | **P4 (M)** |
| Video transforms (`c_fill`/`g_auto` 9:16, trim, poster thumbnails) | Video posts RAW; no normalization; thumbnails Drive-only | Normalize video for STORIES; reduce Meta 9004-class failures; Mini App posters for video | **P5 (M)** |
| `phash` perceptual fingerprint at upload | SHA256 exact-match dedup only | Catch re-encoded/recompressed dupes (Telegram recompresses — same photo via Drive vs Telegram has different SHA256) | **P6 (M)** |
| Image Generation add-on (text-to-image) | n/a | Content-supply backstop for pool-dry days; branded generated stories (compound plays #152/#189) | **P7 (L, exploratory)** |
| Timeouts / async-safety on all Cloudinary calls | None (sync, unbounded, on shared loop) | Required substrate before any expanded usage | **P0 (S)** |

## 4. Proposals

Sizing convention (fleet standard, no calendar estimates): **S** = one focused PR; **M** = one substantial PR or a short series; **L** = multi-PR workstream with product decisions.

### P0 — Bound and offload every Cloudinary call (substrate) — S

- **What:** Pass a finite `timeout` in the Cloudinary config/upload options and move all `cloudinary.*` calls (upload, destroy, Admin `resources`) off the event loop via `asyncio.to_thread`, matching the pattern already used for the caption service's Anthropic call.
- **Why:** The hang/perf knowledge doc (prod-confirmed alongside #557) identifies the unbounded synchronous upload as a whole-bot freeze vector. Every proposal below increases Cloudinary call volume; this must land first.
- **Pipeline mapping:** `cloud_storage.py` only — callers unchanged. Additive to PR #510 Phase 4 (DB-off-loop), which covers the DB side of the same substrate.
- **Risks/open questions:** none material; pick timeout values consistent with the Instagram path's existing 60/10/60+180s budget.
- **Related:** hang-perf doc (fleet knowledge), #557, PR #510 Phase 4. Not covered by any open issue — file one if accepted.

### P1 — Cloudinary as persistent storage for web uploads — M

- **What:** Upload Mini App media to Cloudinary **at receipt** (server-relay, signed), store `cloud_url`/`cloud_public_id` as the primary reference, and demote `/tmp` to a transient staging area (or remove it). Serve Mini App thumbnails for web-upload items from Cloudinary delivery URLs (`q_auto,f_auto,w_...`) instead of the Drive-proxy path (which web uploads never had).
- **Why:** Closes #317 (media loss on every Railway redeploy — silent failure, stale `source_identifier` paths). The model columns already exist; the issue and the code comment both name this exact fix. It also unblocks #184's product goal (Drive-free onboarding) with the smallest first hop.
- **Pipeline mapping:** `dashboard.py:405-466` (`onboarding_upload_media`) gains an upload-to-Cloudinary step after the existing MIME/magic-byte validation and SHA256 dedup gate (both stay server-side — this is why server-relay beats direct-to-Cloudinary as the first hop); `local_provider.py` gains (or is superseded by) a provider whose `download_file` returns bytes from the Cloudinary URL, per #184's `WebUploadProvider` sketch. Post-time `upload_media()` becomes a no-op for already-persistent assets (use the stored URL directly).
- **Sequencing:** requires **P2 first** — today's `cleanup_expired` deletes *everything* in the folder older than 24h by `created_at`; a persistent asset uploaded into the current lifecycle would be reaped within a day.
- **Follow-on (separate M, optional):** #184's signed **direct-to-Cloudinary browser upload** (signature endpoint; dedup/validation move to post-upload checks). Defer until webhook/`notification_url` behavior is verified (§7) — dedup-after-upload depends on it.
- **Risks/open questions:** storage/bandwidth credits become a real cost dimension (free tier = 25 pooled credits/mo; current transient model consumes almost none) — needs a quick usage projection against the actual pool size; keep 50MB cap and tenant-scoped folders (`#515` hardening applies unchanged).
- **Related:** #317 (closes), #184 (first hop of), #152/#189 (enables), #515 (validation stays server-side).

### P2 — Tag-scoped lifecycle via upload presets — S

- **What:** Introduce a named upload preset (or explicit tag parameters) so every upload carries: a lifecycle tag (`story-transient` vs `persistent`), the tenant id as a tag, and `phash: true` (see P6). Switch `cleanup_expired` from folder+`created_at` scans to **tag-scoped** deletion (`story-transient` + age), and either persist `cloud_expires_at` at upload or drop the always-NULL column (consolidate — one source of truth for retention).
- **Why:** Cloudinary has **no native TTL** (verified) — engineered cleanup is the documented pattern, so ours stays; tags make it precise instead of "everything in the folder", which is the precondition for P1's persistent assets to coexist safely. Tag-scoped listing also keeps Admin API usage within the free tier's rate budget (500 req/h per 2025 documentation — current value unverified, §7).
- **Pipeline mapping:** `_build_upload_options()` (`cloud_storage.py:197-216`) gains tags; `cleanup_expired()` (`:271-340`) switches listing strategy; both duplicated pipelines pick the change up automatically since they share `CloudStorageService`. Pair with the #550 fix (work-first-then-sleep) and #450's test file — same loop, one PR closes both.
- **Risks/open questions:** none material; migration is additive (untagged legacy uploads age out within 24h under the old rule kept as fallback during transition).
- **Related:** #450, #550 (both closable alongside), #499 (already fixed — pagination logic carries over), #500 (shared-service change, no new divergence).

### P3 — `q_auto` (+ constrained format) in the story URL chain — S

- **What:** Add `q_auto` to the story transformation chain. For the **Meta-facing** URL, pin an explicit IG-safe format (`f_jpg` for images) rather than `f_auto`; use `f_auto,q_auto` freely on any **browser-facing** URLs (Mini App previews, P1 thumbnails).
- **Why:** Smaller payloads → faster Meta container readiness (the 2s-poll×30 loop in `instagram_api.py:256-300` is a real latency budget), lower bandwidth credits. Cloudinary's July announcement highlights q_auto (newly patented) — it's mature. The `f_auto` caution is deliberate: format negotiation depends on the fetching client's Accept header, and Meta's fetcher behavior is unverified — a WebP/AVIF response to Meta is a plausible new 9004 source (`MediaUnsupportedError` history: HEIC-as-JPG, transform output IG can't decode).
- **Pipeline mapping:** `get_story_optimized_url()` chain string (`cloud_storage.py:410-416`); add the missing direct unit test for the chain output while there (currently only mocked at call sites).
- **Risks/open questions:** verify with a dry-run post (the autopost dry-run mode exists for exactly this).
- **Related:** 9004/`MediaUnsupportedError` handling (CHANGELOG Unreleased), #500 (single shared function — both paths benefit).

### P4 — Generative 9:16 story framing (`b_gen_fill`) as a per-tenant toggle — M

- **What:** Alternative story-framing mode: replace the blurred-underlay letterbox with `b_gen_fill` (content-aware extension to 1080×1920), selectable per tenant (`chat_settings`-level flag, default off, existing chain remains the default).
- **Why:** This is the highest-visual-impact match between Cloudinary's generative suite and our product: letterboxed stories become full-bleed. Toggle shape follows the fleet principle (visual changes net-add, never force-flip).
- **Pipeline mapping:** second branch in `get_story_optimized_url()`; flag on `chat_settings`; surface in the Mini App settings later. **Fallback required:** on any gen-transform failure serve the letterbox chain (transform failures surface as Meta 9004 — we already permanent-reject on those, which would be wrong for a recoverable gen failure).
- **Risks/open questions:** (a) generative transforms bill at a **higher usage multiplier** (verified) and their free/low-tier gating is **unresolved** (§7) — needs a one-image spike on our actual plan before committing; (b) first-derivation latency on a gen transform is nontrivial — mitigate via eager transformation at upload (P2's preset) once assets persist (P1), or accept first-hit cost in the transient model; (c) quality on arbitrary UGC needs eyeballing (dry-run mode again).
- **Related:** none open (new capability). Depends on: P3's test scaffolding, ideally P1/P2 for eager derivation.

### P5 — Video story normalization + poster thumbnails — M

- **What:** Stop posting raw video: normalize to 9:16 via `c_fill,g_auto` (or pad, mirroring the image chain), transcode to an IG-safe MP4 profile, and generate poster thumbnails (`.jpg` on the video URL) for the Mini App grid. Derive **eagerly at upload** (preset) — synchronous video derivation on first URL fetch can return HTTP 423 (async processing), which would break the Meta container flow mid-post.
- **Why:** Video is currently the untransformed half of the pipeline (`story_url = cloud_url` raw). Odd-aspect or oversized videos are a live 9004/quality risk, and video items have no thumbnail story in the Mini App. `g_auto` subject-tracked video cropping is verified current capability.
- **Pipeline mapping:** extend `get_story_optimized_url()` (or a sibling `get_story_video_url()`) — today it's explicitly skipped for video in both pipelines; unify the three scattered media-type checks while there (#500's shared media-type utility).
- **Risks/open questions:** video transform billing multipliers are real (HD ≈ 4 transformation-units/sec per 2023 support data — stale, re-verify); Meta-side STORIES video constraints (duration/codec caps) must be confirmed against current Graph API docs at implementation time.
- **Related:** #500 (media-type utility), #294 (Telegram-to-IG pipeline would inherit it), 9004 handling.

### P6 — Perceptual dedup (`phash`) alongside SHA256 — M

- **What:** Request `phash: true` on every image upload (P2's preset), store the 64-bit fingerprint in a new `media_items.phash` column, and add a Hamming-distance near-duplicate check at ingest (alongside the existing exact-SHA256 409 gate) and to the `dedup-media` CLI.
- **Why:** Exact hashing misses the dominant real-world duplicate class: platform recompression. The same photo ingested via Google Drive and via Telegram (which recompresses server-side) has different SHA256s today and enters the pool twice; the scheduler's lock-hash exclusion (`media_repository.py:635-648`) also misses it.
- **Pipeline mapping:** upload options (`cloud_storage.py`), migration for the column, repo query + ingest check (`dashboard.py:420` area), CLI extension. Linear scan is fine at current pool sizes; no index machinery needed yet (YAGNI).
- **Risks/open questions:** (a) **images only** — no video pHash (verified); (b) Cloudinary normalizes before hashing, so values are **not comparable** with locally-computed `imagehash` values — don't backfill `/tmp`-era assets locally, backfill via Cloudinary `explicit` calls or leave legacy unfingerprinted; (c) requires assets to pass through Cloudinary at ingest → depends on P1 for web uploads (Drive/Telegram items get fingerprinted at first post, or via a backfill pass); (d) threshold tuning is DIY (no vendor-recommended Hamming cutoff — start strict, e.g. distance ≤ 4, and log-only before enforcing). The Duplicate Image Detection *add-on* is *not* proposed — Beta, contact-gated enablement, undocumented pricing.
- **Related:** #184 (its checklist mandates dedup for direct upload), dedup-media CLI, scheduler hash-exclusion.

### P7 — AI image generation for content supply (exploratory) — L

- **What:** Use the new Image Generation add-on as a **content-supply backstop**: when a tenant's pool runs dry (pool-health is already tracked), generate on-brand story images from templated prompts (category/tags/brand context), enter them through the normal queue + approval flow (never auto-post generated content unreviewed).
- **Why:** "Pool runs dry → nothing posts" is a known failure mode (Content Supply Chain compound play #152; Self-Serve Content Platform #189). This is the only July announcement that adds a genuinely new capability class for us, and the shipped AI-caption flow (#182/PR #254: generate → accept/edit/regenerate in Telegram) is the exact UX precedent for AI-in-the-loop.
- **Pipeline mapping:** new integration service beside `CaptionService` (same to_thread/offload pattern); generated assets stored as managed Cloudinary assets (P1's persistent model) with a `generated` tag (P2); queued as `media_items` with a new `source_type='generated'` provider — the `MediaSourceProvider` abstraction accommodates it cleanly.
- **Risks/open questions:** **product-shape decision — needs Chris** (brand voice, disclosure, whether generated content belongs in the product at all); separate credit-based billing (free allotment size unverified, §7); API flagged "early version" by its own docs despite the GA announcement — expect surface churn; prompt quality determines viability (spike first: generate 10 images against a real tenant's categories, human-review).
- **Related:** #152, #189, #182 (shipped precedent), #155 (shipped — auto-approval must *never* extend to generated content without explicit decision).

## 5. Explicitly not proposed

- **Re-fixing cleanup pagination** — done (#499, PR #508).
- **Native TTL / auto-expiry** — does not exist on Cloudinary for uploaded assets (verified); the engineered loop stays. Fetched-asset expiry is a different asset type and paid-only.
- **MediaFlows scheduled-deletion workflows** — adds an external orchestration dependency to replace a loop we already run in-process; tier gating unverified.
- **OAuth migration** — the new self-service OAuth is additive; key/secret signature auth is unchanged and remains the right fit for a server-side backend. No action.
- **Duplicate Image Detection add-on** — Beta, contact-Cloudinary enablement, no documented pricing; `phash` (P6) delivers the capability without the gate.
- **VS Code extension adoption** — developer tooling; individual choice, no repo change.
- **Re-proposing shipped work** — AI captions (#182/PR #254) and smart auto-approval (#155/PR #167) are live; P7 builds on their patterns rather than reopening them.
- **Anything inside PR #510's scope** — the queue/posting simplification plan deliberately excludes media storage/Cloudinary; these proposals stay additive to it (shared substrate only at P0).

## 6. Dev-tooling notes (non-product)

The VS Code extension GA (in-IDE media library, MCP/agent-skill setup) and the new Studio transformation-builder UI are useful for *authoring* the P3–P5 transform chains interactively before codifying them. No repo changes.

## 7. Unverified claims & research gaps (flagged honestly)

- **Upload `notification_url` / webhooks**: not covered by the gathered sources — verify before designing #184's direct-upload dedup-after-upload flow (P1 follow-on).
- **Free/low-tier gating of generative transformations** (`b_gen_fill` et al.): one search snippet suggests Plus-and-up for some generative add-ons; the fetched pricing page doesn't state it. Spike on our actual plan before P4.
- **Image Generation pricing specifics**: free-allotment size and per-model credit costs not captured; GA-vs-early-access wording conflicts between release notes and API docs.
- **Current rate-limit/credit values**: Admin API 500 req/h (free) and the credit-equivalence formula come from 2025 support-article snapshots (live pages 403'd during research); re-confirm at implementation time.
- **Meta-side STORIES constraints** (video duration/codec/format acceptance, fetcher Accept header for `f_auto`): out of Cloudinary's scope; confirm against current Graph API docs during P3/P5.

## Sources

Cloudinary (fetched 2026-07-14): [Programmable Media release notes](https://cloudinary.com/documentation/programmable_media_release_notes) (2026-07-09 entries) · [Image Generation add-on](https://cloudinary.com/documentation/image_generation_addon) · [OAuth for Cloudinary APIs](https://cloudinary.com/documentation/using_oauth_to_access_cloudinary_apis) · [Authentication signatures](https://cloudinary.com/documentation/authentication_signatures) · [Generative AI transformations](https://cloudinary.com/documentation/generative_ai_transformations) · [Transformation reference](https://cloudinary.com/documentation/transformation_reference) · [Upload presets](https://cloudinary.com/documentation/upload_presets) · [Video manipulation](https://cloudinary.com/documentation/video_manipulation_and_delivery) · [Video layers](https://cloudinary.com/documentation/video_layers) · [Deleting temporary UGC assets](https://cloudinary.com/documentation/delete_temporary_ugc_assets) · [Backups & version management](https://cloudinary.com/documentation/backups_and_version_management) · [pHash / semantic data extraction](https://cloudinary.com/documentation/semantic_data_extraction) · [Duplicate Image Detection add-on](https://cloudinary.com/documentation/cloudinary_duplicate_image_detection_addon) · [VS Code extension](https://cloudinary.com/documentation/cloudinary_vscode_extension) · [Pricing](https://cloudinary.com/pricing). Rate-limit/credit support articles readable only via 2025 Wayback snapshots (noted inline).

Internal grounding: pipeline map verified against `main` @ 7c99a34; issues #317/#500/#450/#550/#184/#182/#155 (+ adjacent) reviewed; fleet knowledge docs `hang-perf-regression-2026-06-12`, `queue-item-not-found-regression-2026-06-12`, `system-flow-audit-2026-05-17`; PR #510 plan checked for overlap (none on media/Cloudinary).
