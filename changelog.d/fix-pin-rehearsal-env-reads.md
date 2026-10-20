### Tests

- **The settings guard passes with the cutover rehearsal's variables.** `scripts/cutover_rehearsal.py` reads `REHEARSAL_RESTORE_ADMIN_DSN`, `REHEARSAL_CODE_ADMIN_DSN`, `SCHEMA_DRIFT_DSN` and `REQUIRE_SCHEMA_DRIFT_CHECK` from the environment, and the guard requires every such variable to be pinned and named in `.env.example`. Both now list them, and `.env.example` says what each one is for.
