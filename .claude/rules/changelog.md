---
paths:
  - "CHANGELOG.md"
---

# Changelog Maintenance

**Format**: [Keep a Changelog](https://keepachangelog.com/) with [Semantic Versioning](https://semver.org/).

**Every PR that touches code or config** must include an entry under `## [Unreleased]` — CI's `changelog-check` (`.github/workflows/ci.yml`) fails without it; a docs-only PR (`documentation/`, `*.md`, `.github/`) is exempt.

## Version Bump Rules

- **MAJOR** (X.0.0): Breaking changes, incompatible API changes
- **MINOR** (x.Y.0): New features, backward-compatible additions
- **PATCH** (x.y.Z): Bug fixes, minor improvements

## Entry Format

```markdown
## [Unreleased]

### Added

- **A sentence naming the change, with its refs (#NNNN).** Prose in the same bullet: what it does, why, what a reader must know.

### Fixed

- **What was broken, named as the behaviour (#NNNN).** How it was fixed, in the same bullet.
```

One bullet per change: a bold sentence ending in a period, the issue or PR refs
inside the bold, then prose — no ` - ` separator and no nested bullets.

Categories: `Added`, `Changed`, `Removed`, `Fixed`, `Security` (and `Deprecated`
when it applies); `Documentation` and `Tests` are in use for changes with no
user-facing behaviour.

## Best Practices

- Write from the user's perspective
- Include enough detail to understand the change without reading code
- Reference issue/PR numbers when relevant: `(#123)`
- Group related changes under descriptive subheadings
- For significant changes, add a `### Technical Details` section listing affected files and migrations
