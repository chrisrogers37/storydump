# Changelog fragments

A pull request records its changelog entry here, as one new file, and never
edits `CHANGELOG.md`. Each pull request writes its own file, so no two of them
change the same lines and a merge does not conflict the others.

## The rule

The Changelog Check job runs `python scripts/changelog_fragments.py check
--base origin/main` and holds every pull request to this:

- A pull request that changes anything outside `documentation/`, `.md` files
  and `.github/` adds a fragment. A docs-only one needs none; if it has an
  entry, that is a fragment too.
- No pull request edits `CHANGELOG.md` except the compile below. A correction
  to an existing entry rides in a compile pull request.
- Every fragment parses.

## Writing one

- **Name:** the branch, with `/` written as `-`
  (`branden-1410-changelog-fragments.md`), or the pull request's number
  (`1410.md`). Any new name ending in `.md` works; this README is the only
  `.md` here that is not a fragment.
- **Content:** what `CHANGELOG.md` would have held, its section heading
  included, in the entry style of `.claude/rules/changelog.md`:

```markdown
### Fixed

- **What was broken, named as the behaviour (#NNNN).** How it was fixed.
```

- **Sections:** `Added`, `Changed`, `Deprecated`, `Removed`, `Fixed`,
  `Security`, `Documentation`, `Tests`. A fragment may hold several, each once.
  Those `### <Section>` lines are its only headings: no text before the first
  one, and no `##` or `####` inside an entry.

`python scripts/changelog_fragments.py check` validates them locally.

## The compile

`python scripts/changelog_fragments.py compile` folds every fragment into
`CHANGELOG.md` under `## [Unreleased]`, in file-name order, each entry first in
its section of the newest group (creating a section that group lacks), and
deletes the fragments. Commit `CHANGELOG.md` and the deletions together.

Nothing ties it to a release. No open pull request touches `CHANGELOG.md` or
another pull request's fragment, so a compile pull request conflicts with none
of them and can run whenever `CHANGELOG.md` should catch up. Until then, the
newest entries are here.
