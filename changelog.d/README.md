# Changelog fragments

A pull request that changes code or config records its changelog entry here,
as one new file, and never edits `CHANGELOG.md`. Each pull request writes its
own file, so no two of them change the same lines and a merge does not
conflict the others.

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

`python scripts/changelog_fragments.py check` validates every fragment. The
Changelog Check job runs it with `--base`, which also fails a pull request that
changes code without adding a fragment, or that edits `CHANGELOG.md` beside
code. A docs-only pull request (only `documentation/`, `.md` files or
`.github/`) needs no fragment.

## At release

`python scripts/changelog_fragments.py compile` folds every fragment into
`CHANGELOG.md` under `## [Unreleased]`, in file-name order, each entry first in
its section of the newest group (creating a section that group lacks), and
deletes the fragments. Commit `CHANGELOG.md` and the deletions together: that
pull request is docs-only.
