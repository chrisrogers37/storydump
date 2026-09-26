---
title: "F2 — where the CLI keeps its bearer token (decision)"
type: decision
status: ratified
owner: chris
created: 2026-09-15
tags: [decision, cli, auth, security]
links: []
---

## Summary

Keychain by default where the host has one; fail closed with no plaintext fallback; a 0600 file
only by explicit opt-in (`--insecure-storage`); `STORYDUMP_TOKEN` overrides everything for agents
and CI; the secret is read from a prompt or stdin, never argv; `keyring` ships in the
`storydump[cli]` extra, never in the server's install list. The `gh` pattern (2.24+), which
`stripe` and 1Password's CLI converged on. Ratified by the owner 2026-09-15 after
`weigh-development-paths`.

## Why it matters

The token is a person-bound bearer secret good for up to 90 days across every workspace the
person belongs to. The two things that leak developer tokens in practice — dotfile sync and
backups, and casual reads by other processes running as the same user — are exactly what an OS
keychain resists and a file does not. The fallback question decides the security: `keyring`'s
plaintext `keyrings.alt` fallback on headless hosts is worse than the file it replaces, so the
backend refuses it. Packaging decides the blast radius: the dependency belongs to the CLI extra.

## The matrix

| Dimension | (a) keychain, as first written | (b) file default, keychain extra | (c) env only | (d) keychain default, file opt-in, env override |
|---|---|---|---|---|
| Elegance | one store, fallback unanswered | two stores, the insecure one default | fewest parts, secret in shell profiles | two stores, one order, the safe one default |
| Existing patterns | no client-secret store in the repo; scripts are env-only | matches the Railway CLI's plain file | matches every script | matches `gh`, used daily here |
| Extension | needs an injectable backend for tests | the injectable backend is the design | nothing to extend | same backend interface, defaults inverted |
| DRY | one path | two paths, one precedence rule | one path | two paths, one precedence rule |
| Separation | packaging leaks to the server unless split | clean with the extra | storage becomes the shell | clean with the extra; policy in the backend |
| Future-proofing | a refresh token stores the same way | same | the variable goes stale hourly under short-lived tokens | a refresh token has a safe home |
| Plan alignment | blocked by the lenses (dependency, fallback, packaging) | satisfies the lenses, insecure by default | breaks `login` and `doctor` | satisfies the lenses and the spec |

## The pick

**(d).** What is lost against (b): a native dependency in the CLI extra and a one-time
keychain-unlock prompt on macOS. Against (c): a few dozen lines. What would change it: nothing
foreseeable; if short-lived tokens with a browser login land later, the refresh token stores the
same way.

## Related

- `../00_EPIC.md` — fork F2, locked with this record as evidence.
- `../01_tokens.md` — step 11 (the storage backend, the extra, `login`).

## Origin

Ironclad cycle 1 on PR #1309 (the devex and engineering lenses argued against (a) as written);
`weigh-development-paths` run 2026-09-15; owner's ratification in the same session.
