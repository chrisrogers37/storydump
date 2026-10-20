---
paths:
  - "CHANGELOG.md"
  - "changelog.d/**"
---

# Changelog Maintenance

**Format**: [Keep a Changelog](https://keepachangelog.com/). `CHANGELOG.md` holds one group, `## [Unreleased]`; nothing ties a compile to a release or a version bump.

**Every PR that touches code or config** adds its entry as one new fragment in `changelog.d/`, and no PR edits `CHANGELOG.md`. A PR that changes only `documentation/`, `.md` files and `.github/` needs none (`DOCS_ONLY`, `scripts/changelog_fragments.py`). Naming, the rule CI's Changelog Check job (`.github/workflows/ci.yml`) applies, and the compile that folds the fragments into `CHANGELOG.md`: `changelog.d/README.md`. Check locally with `python scripts/changelog_fragments.py check`.

## Entry Format

A fragment holds exactly this: one or more `### <Section>` headings, each once and with its entries, and nothing before the first heading.

```markdown
### Added

- **A sentence naming the change.** Prose in the same bullet: what it does, why, what a reader must know.

### Fixed

- **What was broken, named as the behaviour (#NNNN).** How it was fixed, in the same bullet.
```

One bullet per change: a bold sentence ending in a period, any issue or PR refs inside the bold, then prose — no ` - ` separator and no nested bullets.

Sections (`SECTIONS`): `Added`, `Changed`, `Deprecated`, `Removed`, `Fixed`, `Security`; `Documentation` and `Tests` for changes with no user-facing behaviour. These are a fragment's only headings: the check refuses any other, so affected files and migrations go in the entry's prose.

## Best Practices

- Write from the user's perspective
- Include enough detail to understand the change without reading code
- Reference an issue or PR number when there is one: `(#123)`
