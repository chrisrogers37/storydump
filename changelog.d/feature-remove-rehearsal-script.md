### Removed

- **The dump-restore rehearsal script, its tests and its four environment variables are gone.** `scripts/cutover_rehearsal.py` is deleted with `tests/scripts/test_cutover_rehearsal.py`, `tests/scripts/test_cutover_rehearsal_cleanup.py` and `tests/scripts/cutover_rehearsal_integration.py`. `.env.example` and the settings guard's pinned set no longer name `REHEARSAL_RESTORE_ADMIN_DSN`, `REHEARSAL_CODE_ADMIN_DSN`, `SCHEMA_DRIFT_DSN` or `REQUIRE_SCHEMA_DRIFT_CHECK`; the live-drift test (`tests/scripts/test_schema_drift_live.py`) still reads the last two. The runner's `parity` function no longer takes the URL names the script passed it, and the `parity` command is unchanged.

### Documentation

- **`documentation/operations/schema-cutover.md` keeps its record of production's database and drops the procedure sections that followed it.** The README, the documentation index and the deployment, cloud deployment and CI/CD guides stop pointing to them.
