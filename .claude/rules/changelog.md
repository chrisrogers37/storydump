---
paths:
  - "CHANGELOG.md"
  - "changelog.d/**"
---

# Changelog Maintenance

**Format**: [Keep a Changelog](https://keepachangelog.com/) with [Semantic Versioning](https://semver.org/).

**Every PR that touches code or config** adds its entry as one new fragment in `changelog.d/`, and no PR edits `CHANGELOG.md`. Naming, the rule CI's `changelog-check` (`.github/workflows/ci.yml`) applies, and the compile that folds the fragments into `CHANGELOG.md`: `changelog.d/README.md`.

## Version Bump Rules

- **MAJOR** (X.0.0): Breaking changes, incompatible API changes
- **MINOR** (x.Y.0): New features, backward-compatible additions
- **PATCH** (x.y.Z): Bug fixes, minor improvements

## Entry Format

A fragment holds exactly this: one or more `### <Section>` headings, each with
its entries, and nothing before the first heading.

```markdown
### Added

- **A sentence naming the change, with its refs (#NNNN).** Prose in the same bullet: what it does, why, what a reader must know.

### Fixed

- **What was broken, named as the behaviour (#NNNN).** How it was fixed, in the same bullet.
```

One bullet per change: a bold sentence ending in a period, the issue or PR refs
inside the bold, then prose — no ` - ` separator and no nested bullets.

Categories: `Added`, `Changed`, `Removed`, `Fixed`, `Security` (and `Deprecated`
when it applies); `Documentation` and `Tests` are in use for changes with no
user-facing behaviour. These are a fragment's only headings: the check refuses
any other, so affected files and migrations go in the entry's prose.

## Best Practices

- Write from the user's perspective
- Include enough detail to understand the change without reading code
- Reference issue/PR numbers when relevant: `(#123)`
