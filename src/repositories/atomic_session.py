"""Shared-session context manager for atomic multi-repo transactions.

Several code paths need a set of repositories to write within a single
database transaction so their effects commit or roll back together (e.g. the
auto-post finalize: write posting_history AND delete the posting_queue row as
one unit, so a crash can never leave a story recorded-but-still-queued or
published-but-unrecorded).

``atomic_session`` swaps every repo onto one shared session, redirects each
repo's ``commit()`` to ``flush()`` so intermediate writes accumulate without
committing, then does a single real commit at the end (or rolls back on any
failure). This is the primitive behind ``TelegramCallbackCore._shared_session``
and the scheduler / autopost finalizes.
"""

from __future__ import annotations

from contextlib import contextmanager


@contextmanager
def atomic_session(repos):
    """Run ``repos`` on one shared session, committing once at the end.

    Args:
        repos: A non-empty sequence of ``BaseRepository`` instances. The first
            repo's session becomes the shared (primary) session; the rest are
            temporarily rebound to it and restored on exit.

    The block is atomic: if it raises, the shared session is rolled back and
    the exception propagates; otherwise a single commit persists everything.
    """
    repos = list(repos)
    primary_session = repos[0].db
    originals = {}

    # Point every repo at the shared session.
    for repo in repos:
        originals[id(repo)] = repo._db
        repo.use_session(primary_session)

    # Defer intermediate commits: repo.commit() -> session.flush().
    original_commit = primary_session.commit
    primary_session.commit = primary_session.flush

    try:
        yield primary_session
        # Everything succeeded — do the one real commit.
        original_commit()
    except Exception:  # noqa: BLE001 — roll back on any failure, then re-raise
        primary_session.rollback()
        raise
    finally:
        # Restore the real commit and each repo's own session.
        primary_session.commit = original_commit
        for repo in repos:
            if id(repo) in originals:
                repo.use_session(originals[id(repo)])
