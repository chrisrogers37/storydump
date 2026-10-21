### Changed

- **Instagram past posts is on wherever its key ring is, with no record files and no switch.** A connected professional account's past feed posts become a posting source once the API and the worker hold `PAST_POSTS_HMAC_KEYS`, which they already need for Meta data-deletion receipts.
  - **The run.** Each release carries its own run (`release-<commit>`), which keys the source-mix conversion jobs and the existing-account reconciliation jobs, so a deploy retries whatever an earlier one could not finish.
  - **Workspaces.** A workspace still on the folder-keyed posting mix is converted to the source-keyed one, and a new workspace starts converted.
  - **Accounts.** An account connected before the past-posts source was on gets its past-posts source added Off, to be switched On on the posting-mix card. A new connection's History still defaults On.
  - **Videos** stay `validation_pending` until the video checks have real evidence, so only images reach the rotation.

### Removed

- **`INSTAGRAM_ARCHIVE_ACTIVATION_RECORD` and `SOURCE_MIX_CONVERSION_RECORD`.** Both were operator record files tied to the exact commit running, so every deploy would have switched the past-posts source off again. Neither is read any more, and a leftover value changes nothing.

### Documentation

- **The past-posts runbook.** `instagram-past-posts-review.md` now says how Instagram past posts turns on (the ring alone) and what each account sees. Its activation prerequisites became a list of what is still unproven on real providers. The Meta section explains that accounts you own connect under Standard Access, and that App Review is needed before anyone else's account can connect.

### Tests

- **The activation tests pin the ring as the only input.**
  - A whole ring arms the past-posts source on any release.
  - A missing or malformed ring names its reason.
  - The release run fits the shapes `fn_mix_conversion_jobs` and `fn_past_posts_reconcile_jobs` accept.
  - A leftover record variable neither arms nor disarms anything.

  The composition, worker and gate tests now arm with the ring alone.
