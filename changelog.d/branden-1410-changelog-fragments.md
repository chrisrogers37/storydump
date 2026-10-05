### Changed

- **A pull request records its changelog entry as a file in `changelog.d/` instead of editing `CHANGELOG.md`, so a merge no longer conflicts every open pull request (#1410).** Every entry went in at the same place in `CHANGELOG.md`, so each merge re-conflicted the open pull requests there, and each conflict cost a main merge, a CI run and a re-review. The Changelog Check now runs `scripts/changelog_fragments.py check --base`: a pull request that changes anything outside the docs fails without a new fragment, and any pull request that edits `CHANGELOG.md` fails unless it is the compile. `scripts/changelog_fragments.py compile` folds the fragments into `CHANGELOG.md` under `[Unreleased]`, each entry first in its section, and deletes them; nothing ties it to a release. The rule and the format are in `changelog.d/README.md`.

### Fixed

- **A failed Codecov upload failed the Test check, so the production deploy, which waits on CI, was skipped (#1594).** The upload is advisory, and `fail_ci_if_error: false` did not cover every failure (a TLS handshake failure failed the step regardless), so the `Upload coverage to Codecov` step in `.github/workflows/ci.yml` now sets `continue-on-error: true`, with a five-minute timeout so that a stalled upload cannot hold the job either.
