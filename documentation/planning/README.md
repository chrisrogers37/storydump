# Planning — live plans only

This folder holds plans and specs for work that is not finished. When a plan completes — or is
superseded or abandoned — it moves to [`../archive/`](../archive/README.md) **under the same
folder name**, with a one-line status banner at its top and a row in the archive's index.

So a `documentation/planning/<name>` path you are holding — from an old link, a commit message
or a migration's header — is found at `documentation/archive/<name>`.

Migrations are the one place such a path cannot be corrected: the runner checksums every applied
file ([`../operations/migration-runner.md`](../operations/migration-runner.md)), so a header that
cited a plan while it was live keeps that path. `tests/test_doc_links.py` holds each one to
resolving either as written or under `documentation/archive/`, which is what makes keeping the
folder name the rule rather than a habit.
