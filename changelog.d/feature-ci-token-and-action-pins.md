### Changed

- **CI's token reads the repository and nothing more, and no checkout keeps it.** `.github/workflows/ci.yml` sets `permissions: contents: read` for every job, whatever the repository's default, and `persist-credentials: false` on every `actions/checkout`, so later steps do not find the token in the clone.
- **CI's actions are pinned to full commit SHAs.** Each GitHub-owned action runs the commit of the release it already used, named in a trailing comment, and a new `.github/dependabot.yml` gathers their newer releases into one weekly pull request.
