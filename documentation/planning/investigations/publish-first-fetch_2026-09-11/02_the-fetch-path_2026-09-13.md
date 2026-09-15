---
title: "The story frame's first fetch, continued — what Meta is answered, and by whom (2026-09-13)"
type: audit
status: in-progress
owner: chris
created: 2026-09-13
---

## Summary

The 2026-09-11 fixes (eager frame, readiness check, no lock on a 9004) did not end the first-fetch failures: on 2026-09-12 three of five first calls to Meta still failed with 9004/2207052 in under a second, and every retry that came five minutes or more later succeeded on the SAME url, while the one retry that came 62 s later failed again. This document records what the ledger, the removed deploy's logs, Cloudinary's Admin API and the CDN itself say, what is ruled out, the mechanism the evidence supports, the one experiment that can settle it, and the robustness changes that follow whichever way it settles.

Read with `00_INVESTIGATION.md` (the incident and the first fixes) and `01_first-fetch-and-failure-reporting.md` (what was built).

## Evidence

All times America/New_York. Production rows were read with SELECTs only.

### 1. Every container call the target path has made

| Day | Calls | Failed 9004 | Success took | Failure took |
|---|---|---|---|---|
| 09-10 | 4 | 1 | 6.2–7.7 s | 0.6 s |
| 09-11 | 13 | 3 | 4.5–8.1 s | 0.3–0.5 s |
| 09-12 | 8 | 4 | 3.0–4.6 s | 0.5–1.0 s |

25 calls, 8 failures. Every failure is the FIRST Meta fetch of a freshly uploaded transit asset. Every failure carries the same answer, `9004/2207052: Only photo or video can be accepted as media type.` Retries on the same url: +62 s failed again (photo-output 105); +301 s posted (102); +3602 s posted (103). Files that failed on 09-11 posted first-try that evening as fresh uploads (6 of 6). Success and failure are not separated by file, size, position in the burst, or the seconds between upload and the call (4–5 s in every case).

### 2. The asset Meta was refused (photo-output 105), read from Cloudinary's Admin API on 09-13

- Uploaded 2026-09-12 16:14:48Z as `type=authenticated`, jpg 2048 × 1996, 365,819 bytes.
- Exactly one derived asset: the story frame, transformation IDENTICAL to the delivery url's, 137,673 bytes. The eager derivation and the url Meta is given name the same object; nothing was derived on the fly.
- The delivery host is `res.cloudinary.com`, served by Cloudflare (`server: cloudflare`), `cache-control: public, no-transform, immutable, max-age=2592000`.
- From this machine: HEAD 200 (1.91 s cold), GET 200 with a complete JPEG (0.23 s), GET as `facebookexternalhit/1.1` 200, ranged GET 206. Every client we can be sees the frame.

### 3. The worker's own check passed every time

The removed deploy's logs (Railway deployment 82e889dd, 16:12–16:40Z) carry the three "Meta could not fetch the frame on the first attempt" warnings and never once the "story frame is not serving yet" warning: the readiness HEAD answered 200 image/jpeg on its first poll before each of the three refused calls. The same logs hold the seven `InFailedSQLTransactionError` crashes (the key-4 collision, fixed in #1301) with reschedules of 50–71 s, 271–332 s, 800–997 s and 4157 s — the delays, not the refusals.

### 4. What Cloudinary's CDN answers when it does not know an asset (public demo cloud, no credentials, 09-13 11:45)

- A miss is **HTTP 404 with `content-type: image/gif`** (a placeholder image) and `x-cld-error: Resource not found`, answered in 0.03–0.4 s.
- Although that answer says `cache-control: private, no-cache, max-age=0`, the edge **re-served the identical answer (same `x-request-id`) for at least 80 s**, alternating with a `text/plain NOT_FOUND` variant for two more minutes.
- A cold on-the-fly derivation of an existing public asset: 200 in 0.49 s.

### 5. What Meta documents

"We cURL media used in publishing attempts, so the media must be hosted on a publicly accessible server at the time of the attempt." JPEG is the only image format accepted. 2207052 is "The media could not be fetched from this uri".

### 6. What the ledger records today

The permit row keeps `{"error": 9004}`; `post_intents.last_error` keeps the message. Meta's `error_subcode`, `error_user_msg` and `fbtrace_id` are parsed or available in the adapter but not stored; the readiness probe's observation (status, content type, length, request id, elapsed) is not stored anywhere. This investigation had to re-derive from logs what the ledger should have said.

### 7. The legacy path, same cloud, same host, same account (read 09-13 after the Railway login was restored)

The legacy scheduler posted **1,609 stories through the Instagram API** from January to August 2026, calling Meta 1–3 s after an upload with a PUBLIC, unsigned, on-the-fly `type=upload` url on the same `res.cloudinary.com` cloud. It recorded every Meta 9004 as a `permanent_reject` lock: **20 in eight months, 1.2 % of API posts**, and the files are the kind Meta genuinely refuses (iPhone `.JPG` files that carry HEIC, two `.mov` videos sent raw). The target path's rate is **8 of 25, 32 %**, every one a sub-second first fetch that a later retry accepted. Same Meta, same cloud, 25× the rate: the difference is on our side of the url.

| Path | Delivery url | Frame | API posts | 9004 |
|---|---|---|---|---|
| Legacy (Jan–Aug) | `type=upload`, public, unsigned | on the fly | 1,609 | 20 (1.2 %), file rejects |
| Target (09-10 → 09-12) | `type=authenticated`, signed | eager | 25 | 8 (32 %), first fetches |

### 8. Fresh uploads on the real cloud, from this machine (09-13, throwaway folder, destroyed after)

- Three fresh `type=authenticated` uploads with the eager frame: the pipeline's HEAD answered 200 within 0.1–0.2 s of the upload returning; every GET after it, including eight per trial with a cache-busting query string and two with a different version segment, answered 200 with a complete JPEG.
- Every one of those answers carried the HEAD's own `x-request-id`: **a query string or the version segment does NOT change the cache key on Cloudinary's CDN.** The only way to force a different key is a different transformation string (a no-op step such as `dpr_1.0`, which is also a separate derived asset).
- A url fetched BEFORE its asset existed was answered 404 (image/gif) and the second fetch was served from cache; four seconds after the upload the same url answered 200 with a new request id. At this edge the negative entry did not outlive the upload.

## Ruled out, with the evidence that rules it out

- **The file.** The same files posted later, some as the same url.
- **A mismatch between the eager frame and the delivery url.** Identical transformation, one derived asset.
- **The url's shape.** Commas, colons, the `v1` version segment, Meta's user agent: all serve 200.
- **Our readiness check.** It never failed; it also never proves what Meta sees (a HEAD from the worker's network, headers only).
- **Meta's publish cap, the worker's crashes, deploys.** The cap stood at 2–5 of 100; the crashes delayed jobs but every refused call was a clean call that Meta answered.

## Mechanism

**Best supported:** Meta's cURL reaches a Cloudinary delivery path that does not yet know the fresh asset, is answered the placeholder GIF, and reports it as "only photo or video can be accepted" (a GIF is neither) within a second. The CDN edge on Meta's path then reuses that negative answer for minutes, so a retry inside the window fails again and a retry outside it succeeds. It fits the message, the sub-second timing, the first-fetch-only pattern, the 62 s re-failure, the 5-minute and 60-minute successes, and why no client of ours ever sees it: the worker's HEAD and our probes travel a different path, and the HEAD does not read a body.

**Sharpened by §7:** the legacy path handed Meta the same cloud and host for 1,609 posts with a 1.2 % refusal rate made of real file rejects; the target path hands Meta a `type=authenticated`, signed url and is refused 32 % of the time on first fetch. A public on-the-fly url needs only the original bytes, which the store holds consistently; a signed authenticated url has to be validated against the asset's record on whichever delivery path Meta's edge reaches. If that record lags the upload by seconds on some paths, Meta is answered the placeholder and we never are.

**Alive but less likely:** (b) Cloudflare bot mitigation intermittently challenging Meta's fetcher on `res.cloudinary.com` — weakened by §7, since the legacy used the same host for eight months; (c) a fetch cache on Meta's side (explains the 62 s re-failure, not the first refusal).

The experiment below separates them, and its public arm tests the fix directly. None of them is visible from our side of the CDN.

## The decisive experiment — first run (09-13 12:40, from the developer machine, approved by the owner)

Four library JPEGs the page had already posted on 09-12 (photo-output 104, 103, 102 and IMG_7452), read from Drive through the worker's own door, each uploaded twice (authenticated as production does; public as the legacy did). **Eight container calls, eight accepted**, 2.7–8.1 s each; the readiness HEAD answered 200 on both urls every time; every upload destroyed afterwards; nothing published.

What that run does and does not say. It ran from the developer's machine, so the upload, the readiness HEAD and the Meta call originated on a different network from production's; Meta's own path to the CDN was the same. Eight acceptances at the same hour of day the 09-12 refusals happened rule out "this hour" and "these files", and show that from this vantage a fresh signed authenticated url is accepted within four seconds of upload. They do not reproduce the worker's vantage (Railway `us-east4`), and the refusals only ever appeared from there. The same script, launched inside the worker container, is the faithful rerun (`railway ssh --service worker -- "$(cat remote_cmd.txt)"`, prepared); it waits on the owner because the session's permission gate refuses a remote write into the production container.

## The decisive experiment (design)

Container creation only — `POST /{ig-user}/media` with `media_type=STORIES` — never `media_publish`. A container that is never published appears nowhere and expires in 24 hours. Script: `meta_fetch_experiment.py` (scratch), which contains no publish call.

Per trial, one throwaway JPEG is uploaded twice under a throwaway `ws/<uuid>` folder: once exactly as production does (`type=authenticated`, signed, eager frame) and once as the legacy did (`type=upload`, unsigned, the same eager frame). The pipeline's readiness HEAD runs on both, then the container call is made for each, in alternating order. On a 9004: at once, the same original through a no-op variant of the chain (`dpr_1.0`: a different signed url, a separate derived asset, identical bytes); then the plain url at +30 s and, if still refused, at +120 s. Four trials by default, eight if the first four are ambiguous; every transit asset is destroyed at the end.

What each outcome means:

- Authenticated refused, public accepted, same file, same seconds → the url type is the cause; the fix is F0 below.
- Both refused, the no-op variant accepted at once → the edge's negative entry was the blocker, not the origin.
- Both refused, the variant refused, the plain url accepted at +30 s or +120 s → the origin path did not know the asset yet; the window is measured.
- Nothing refused → rerun from the worker's own container (the vantage the refusals came from); if still nothing, instrument production (F2, F3) and let the next real burst be the experiment.

If the owner prefers zero Meta calls first: Cloudinary's console (Reports → delivery errors) for 2026-09-12 16:14:52Z, 16:15:54Z, 16:16:07Z and 16:33:59Z, and 2026-09-11 16:37:34Z, 16:37:49Z, 20:02:47Z — a 404 on a `ws/…` url at those seconds confirms the mechanism with no experiment at all.

Quota: Meta's published-post quota counts publishes; container calls are expected not to count, and at most ~24 calls is small against 100 either way.

## What makes the system robust, whichever way the experiment settles

In value order. F1 and F3 are the ones this investigation would have been a ledger query with.

- **F0 — Hand Meta the url shape that posted 1,609 times.** If the experiment's public arm is accepted where the authenticated arm is refused, the transit asset Meta reads becomes `type=upload` with the unguessable `ws/<workspace>/<20 hex>` id it already has, destroyed on post and swept by the TTL as today. What is given up is the signature on the url (FC-3.2 as amended by D38): the time-limit property already lives on the ASSET, not the url, and the id has 80 bits of entropy. Requires an amendment to `07` and the FC-3 rows, stated rather than slipped in.
- **F1 — Own the fetch path, or at least see it.** Serve the story frame to Meta from our API: `GET /f/<signed, short-lived token>` streams the derived JPEG from Cloudinary server-side, from the region the worker just verified. Meta's path then has no CDN edge and no negative cache, every fetch is logged (user agent, status, bytes, elapsed), and a failure becomes observable rather than inferred. The worker can read the whole derived JPEG once and verify its bytes before Meta is told. Cost: one route, a token, ~150 KB–20 MB streamed per story.
- **F2 — The readiness probe proves bytes, not headers.** **Built 2026-09-13:** `transit.ready()` answers a `Readiness` carrying the last probe's observation; the default probe is a ranged GET of the first 1,024 bytes under the floor's cap, judged by the JPEG/PNG/MP4 signature (`serves_media`). (A probe-specific cache key is not possible: per §8 neither a query string nor the version segment changes the CDN's key; the probe and Meta read the same object, which is what we want to prove.)
- **F3 — Record the evidence when it happens.** **Built 2026-09-13:** the container permit's `response_ref` carries `elapsed_ms`, `probe` (status, content type, length, request id, elapsed, polls, magic) and on a refusal `meta` (subcode, user title and message, `fbtrace_id`, HTTP status); `post_intents.last_error` carries the subcode and trace id; the adapter's typed error carries Meta's whole answer as `detail`, every field scrubbed of the token.
- **F4 — A fresh cache key per attempt** (**built 2026-09-14** as F4′, `03_the-float_2026-09-14.md` D1: `transit.delivery_url(variant=n)`, one `dpr_1.0` step per refusal, three fresh urls per round) in the url Meta is given, if Cloudinary stays on Meta's path: no negative answer can be reused across attempts. Per §8 the key must change in the TRANSFORMATION (a no-op step such as `dpr_1.0`, one extra derivation per retry); a query string or the version segment changes nothing.
- **F5 — Rungs indexed by fetch failures only.** (**Built 2026-09-14**, plan 03 D2: counters per failure class on the story.) `post_intents.attempts_by_step` existed for this and was unused; today the rung is indexed by every attempt the job ever consumed, which is how a collision pushed a fetch retry to the 60-minute rung.
- **F6 — Bound the slot hold.** (**Built 2026-09-14**, plan 03 D3: every wait steps `publishing → approved`, migration 076.) A story waiting on a rung kept `uq_publish_exclusive` for the whole wait (it stayed `publishing`). With F4 the fetch ladder can be short (20/40/80 s, then the review card); releasing the slot instead means a `publishing → approved` rewind the reconciler must understand — a design decision, not a patch.
- **F7 — Burst serialization at claim time** (later): publish jobs already carry `ig:<account>` as their serialization key; a claim that skips a key with a leased sibling makes bursts collision-free without the 20 s poll.

## Blocked

- The decisive experiment: it makes container calls on the owner's real account, which is a posting-related action and waits for the owner's explicit approval. (The Railway login was restored on 09-13; the legacy record and the two Cloudinary experiments above ran once it was.)

## The evening of 09-13: a real burst with the evidence on, and the twelve-trial run from the worker

**The burst (23:13–23:40, the owner tapping):** 26 taps in 95 s, 17 approved, 9 skipped, every tap answered. Every container call now carries the worker's own read of the frame and Meta's answer (#1302, deployed 22:04).

| Story | The worker's read, just before the call | Meta | Elapsed |
|---|---|---|---|
| A8A4EA44, first | JPEG, 133 KB, 46 ms | refused 9004/2207052 | 704 ms |
| A8A4EA44, +32 s | JPEG, 166 ms | accepted | 3.2 s |
| 0F1CF39F, first / +31 s / +92 s | JPEG each time | refused / refused / accepted | 378 / 538 / 3001 ms |
| BC4F427C, first / +31 s / +92 s | JPEG each time | refused / refused / accepted | 307 / 589 / 3756 ms |
| nine others, first | JPEG, 40–173 ms | accepted | 2.8–4.8 s |

Meta's full text, now on the ledger: title "Media download has failed. The media URI doesn't meet our requirements.", message "The media could not be fetched from this URI: <the exact url we sent, signature intact>", `is_transient: false`, HTTP 400, a `fbtrace_id` each time. So Meta's own fetch fails, on a url the worker read as a valid JPEG from the same region within the previous second, and the same url is accepted 30–90 s later. Retries at ~30 s failed 3 of 4 times; at ~90 s succeeded 2 of 2.

**A second Meta-side surprise:** BC4F427C's container, reported ready, was answered `24/2207006 "The requested resource does not exist"` on the publish call four seconds after creation. The pipeline classed it retryable (right) but the two fetch refusals had spent attempts, so it landed on the 15-minute rung with the same container id, holding the account's slot while seven siblings polled behind it; the retry at +15 min published that same container. Meta's own guidance for 2207006 is to create a new container.

**The twelve-trial run (23:1x–23:2x, inside the worker container, real already-posted files, each uploaded twice, 24 assets destroyed, nothing published):**

| Arm | Accepted first try | Refused |
|---|---|---|
| Public, unsigned, on the same eager frame (the legacy shape) | 12 of 12 | 0 |
| Authenticated, signed (the production shape) | 10 of 12 | 2 |

Trial 1 (photo-output 103): authenticated refused at 0 s; a different url and a separately derived frame of the SAME original (`dpr_1.0`) refused at once; the plain url refused at +30 s; accepted at +150 s. Trial 2 (IMG_7452): refused at 0 s; the different url accepted 5 s later; the plain url accepted at +30 s. The public copy of the identical bytes, uploaded seconds earlier, was accepted every time.

**What last night seemed to settle, and what the morning run took back.** Over 49 first fetches of authenticated assets Meta had refused 13, ≈ 27 %, against none of the 12 public copies and the legacy's 1.2 %, so the authenticated path looked like the difference. **The 09-14 09:20 run (twelve trials, three arms per trial: authenticated signed, public unsigned, public signed) refused all three at the same rate.**

| Arm, 09-14 morning, inside the worker | Accepted first try | Refused |
|---|---|---|
| Authenticated, signed (production) | 8 of 12 | 4 |
| Public, unsigned (legacy shape) | 8 of 12 | 4 |
| Public, signed | 9 of 12 | 3 |

Combined over both runs: authenticated 6 of 24 refused, public unsigned 4 of 24, public signed 3 of 12 — last night's clean public arm was within chance (a 17 % rate gives 0 of 12 about one time in nine). **The delivery type is not the difference.** What holds across every run: the refusal is on a freshly uploaded asset within its first minutes, on a url the worker had just read as a valid JPEG; a refused url tends to stay refused for 30–120 s (accepted at +30 s in 10 of 17 retries, at +120 s in 4 of 6); and a DIFFERENT url of the same original (`dpr_1.0`, a separately derived frame) was accepted at once in 5 of 6 tries. The legacy's 1.2 % over eight months is real but from another era of Meta's and Cloudinary's infrastructure and is not reproducible; it no longer points at the url type.

**Mechanism, restated:** something between Meta's fetcher and Cloudinary's CDN refuses a fresh asset's first fetch about a quarter of the time and remembers the refusal per url for a minute or two. Whether the memory is at the CDN edge or inside Meta's fetcher is not visible from our side; the fresh-url success says it is keyed by url, not by asset.

**What follows for the fixes.** F0 (a public transit asset) is withdrawn: it does not avoid the refusal, and there is no constraint ruling to ask for. **F1 — serve the frame to Meta from our own API** is the structural fix: Meta's path then has no CDN at all, the bytes come from the side that has read them (the readiness probe has never once failed from the worker's vantage), and every fetch Meta makes is logged. **F4′ — on a refusal, retry at once with a fresh derived url of the same original** (5 of 6 accepted immediately; one extra derivation) is the cheap mitigation that can ship first, alongside F5, F6 and F8.

**Added to the list tonight:**
- **F8 — a publish-time `24/2207006` recreates the container** (**built 2026-09-14**, plan 03 D4) (step back to `transit_uploaded`, as the dead-container path does) on its own rung, rather than re-publishing an id Meta says it cannot find.
- **F5 and F6 are no longer theoretical:** an unrelated failure inherited the 15-minute rung from two fetch refusals, and seven stories waited behind one for that long.
- **F9 (nit) — `post_intents.last_error` is not cleared when a story posts** (**built 2026-09-14**, plan 03 D6)**;** BC4F427C reads as posted with a code-24 error beside it.

## The float, live — the first burst on the new worker (2026-09-15 14:54–14:58 UTC)

The owner tapped through the open board — 20 cards: 13 approved, 7 skipped — about ninety minutes after the worker took the float (PR #1306, `e7ed7aa`, 076 applied by the pre-deploy runner). Read from the ledger only (`audit_events`, `provider_operations`, `daily_post_counts`, `channel_outbox`):

- **20 taps, 20 outcomes, none lost:** 13 `posted`, 7 `skipped`; every posted card reads "✅ Posted · HH:MM America/New_York". The one card open afterwards was minted by the clock at 15:06, after the burst.
- **21 container calls, 8 first-fetch refusals** (`9004/2207052`, 38 % of calls — above the ~25 % of the experiments): six of the thirteen stories were refused at least once; one was refused three times in a row (variants 0, 1, 2 within six seconds) before its fourth url was accepted. **Every refusal cleared inside the same run with a fresh url, 3–4 s later: 8 of 8** (the experiments said 5 of 6). The fetch ladder never had to wait.
- **The float engaged once, on another class:** a story whose container had been accepted on its second url was answered `MetaRetryableError 9007` at the publish call (the container not yet available). It stepped back (`float_wait`: class `retry`, rung 1, 60 s), **five siblings posted past it**, it re-entered 99 s later (the slot was busy — the 20/40/60 s back-off) and posted in 3 s. Its card read Approved, then Posted; no notice. On 2026-09-13 the same shape held seven stories for fifteen minutes.
- **13 stories in 3 min 34 s** (~16 s each); one debit per story (the day's row reads 13/20 — the re-entrant flip did not double-debit); no review card, no failure, nothing mid-flight afterwards.
- **Order:** posting order equalled tap order for 12 of 13; the story that waited posted last — the float's trade, by design (a waiting story lets its siblings pass). Tap order followed the served order (the older cards first).

**What this settles.** F4′ — a fresh url at once — is the mitigation, and it cleared every refusal today. F5/F6 behaved as designed on the one wait (its own class counted, the slot released, siblings through). F9: the posted cards carry no error. **Not exercised live, gate-proven only:** the fetch wait ladder (never needed), a container gone (`24`), the cap line (13 of 20), the review card, video, the reaper's approved leg (nothing stale).

**Open after the burst.**
- **F1 — hold.** Fresh urls cleared 8 of 8; serving the frame from our own API is justified only if refusals start outlasting a round. Decide after a week of `float_wait` rows (the read is `state = 'approved' AND cap_consumed_on IS NOT NULL`, `detail->>'event' = 'float_wait'`, permits with `url_variant > 0`).
- **Ordering as a product guarantee** (owner's observation, 2026-09-15): a "post these in order" mode would have to hold a waiter's followers behind it — the head-of-line blocking the float exists to remove — so it must be an explicit choice per sequence, never the default. Posting order already follows approval order for every story that does not wait.
- **Video** offers no fresh urls (an encode each) and waits with its url; measure refused videos before eager variants.
- The refusal rate itself (38 % today) is worth a weekly read.
- **Seen in the burst, fixed the same day:** the last card served showed twice — a send whose answer was lost was re-sent by policy, and the twin kept live buttons because the ledger had no id for it (the row: two attempts, one ref; 3 of 118 cards in a week). A tap on such a twin now adopts it as a card of the story and edits it at once (`outbox.adopt_card`, the heal-on-tap PR); an untouched twin still sits with buttons until touched — Telegram gives a bot no way to find a message whose id it never received.

## Verification checklist

- [x] Experiment run once from the developer machine: 8 of 8 accepted (above).
- [x] Experiment rerun from inside the worker container: public 12 of 12, authenticated 10 of 12 (above).
- [x] One more arm: public AND signed — refused 3 of 12, like the others (09-14 morning). F0 withdrawn.
- [x] Legacy record read: 1,609 API posts, 20 `permanent_reject` locks (§7).
- [x] F4′, F5, F6, F8 and F9 landed together as one change — the float (`03_the-float_2026-09-14.md`, 2026-09-14): the owner ruled them one behaviour, not five patches. The l5 gate reproduces the refused fetch: a first call refused `9004`, the next url accepted; three refusals in a row, the story steps back and waits; six waits, the review card.
- [x] The float measured live once (2026-09-15, above): 8 of 8 refusals cleared by a fresh url, one retryable answer floated out of the slot, 13 of 13 posted, none lost.
- [ ] F1 (the frame served from our own API) is decided after a week of `float_wait` rows; today's read says hold. F7 (burst serialization at claim time) stays later.
