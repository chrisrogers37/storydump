### Changed

- **CI runs the test suite in five parallel jobs, and a merge reaches a deploy in about a quarter of the time.** The single `Test` job took 30 to 54 minutes, and Railway waits for CI before deploying. pytest-split now divides the suite by recorded test times (`.test_durations`) across five `Test shard` jobs, each with its own PostgreSQL. Every shard fingerprints the whole suite it collected, and the final `Test` job passes only if all five shards passed and all five fingerprints match, so each test runs exactly once. `Test` also shows the combined coverage on the run's summary page and publishes the merged test times as the `test-durations` artifact, for refreshing `.test_durations` when the shards drift apart.

### Removed

- **The Codecov upload.** It never reached Codecov (tokenless uploads are refused); coverage is now combined on the run's summary page instead.

### Tests

- **The callback-token refusal cases have fixed ids.** Four of them embedded a fresh uuid in their test ids, which would differ between CI's shards; they are now named, and what they check is unchanged.
