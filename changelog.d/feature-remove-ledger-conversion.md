### Removed

- **The migration runner's `convert-ledger` command and its retired-history refusal are gone.** `convert_ledger`, the `convert-ledger` verb and `tests/scripts/test_migration_runner_convert_ledger.py` are deleted, and `apply`, `adopt`, `repair` and `status` no longer classify a ledger as retired-chain history. Every recorded version is still checked against the tree: one with no file or a different checksum fails `apply` and `adopt` and is listed by `status`, and `repair` records the exception it is asked for. `apply --manual`, `--confirm-archive-backup` and the `-- runner:manual` gate are unchanged.

### Tests

- **The test that a recorded version with no file fails `apply` and `adopt`, bounded or not, moves into `TestIntegrity` in `tests/scripts/test_migration_runner.py`.** It keeps its four cases and its checks, without its assertion about the removed command.

### Documentation

- **The docs and the never-run lists describe `001` and `002` as adopted by a fresh build, without the converted-database case.** README.md, AGENTS.md, CLAUDE.md, the `.claude` pages and the deployment, cloud deployment and CI/CD guides no longer name `convert-ledger`.
