"""Base repository class with proper session management."""

import contextvars
from typing import Optional

from sqlalchemy import or_
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from src.config.database import get_db
from src.repositories.tenant_scope import (
    SYSTEM_SCOPE,
    TenantScope,
    require_tenant_context,
    write_allowed,
)
from src.utils.logger import logger
from src.utils.resilience import db_circuit_breaker


#: The one task-local home for every repository's session state: an immutable
#: copy-on-write mapping {(instance sentinel, slot): value}. One ContextVar for
#: the process, whatever the test or repo churn — see
#: BaseRepository._ensure_session_vars for why per-instance vars are forbidden.
_REPO_SESSIONS: contextvars.ContextVar = contextvars.ContextVar(
    "repo_sessions", default=None
)


class BaseRepository:
    """
    Base class for all repositories.

    Handles database session lifecycle to prevent connection pool exhaustion.
    Sessions are created on demand and must be closed when done.

    Integrates with a circuit breaker so that when the database is
    unreachable, subsequent operations fail fast instead of hanging
    for 30 seconds on the pool timeout.

    IMPORTANT: Always call commit() after write operations and
    end_read_transaction() after read-only operations to prevent
    "idle in transaction" connections.
    """

    def __init__(self):
        self._ensure_session_vars()

    def _ensure_session_vars(self):
        """Mint this instance's session key if absent.

        Session state is task-local: each asyncio Task (every PTB
        ``concurrent_updates`` callback) and each ``asyncio.to_thread``
        offload copies the current context, so each opens and owns its OWN
        Session instead of sharing this singleton repo's one Session — a
        SQLAlchemy Session is not safe for concurrent use.

        The state lives in ONE module-level ContextVar holding an immutable
        copy-on-write mapping, never in per-instance ContextVars: a thread's
        Context strongly references every var ever set in it, so per-instance
        vars accumulated forever (two per repo construction — a test session
        mints thousands) and that growth trips CPython 3.10's HAMT collision
        bug as ``TypeError: unhashable type: 'hamt_bitmap_node'`` — the exact
        signature that turned CI red while 3.11 (fixed HAMT) stayed green.
        Task-copy semantics are identical: a task's first write replaces the
        mapping in ITS context only; the mapping itself is never mutated in
        place.

        The key is a per-instance sentinel OBJECT (not ``id(self)``): the
        mapping keeps the sentinel alive, so a recycled ``id`` can never read
        a dead instance's session. Guarded (not inline in ``__init__``) so
        the key still exists for code paths that bypass ``__init__`` —
        notably tests that patch ``__init__`` to a no-op and then assign
        ``_db`` directly.
        """
        if getattr(self, "_session_key", None) is None:
            self._session_key = object()

    def _ctx_get(self, slot: str):
        mapping = _REPO_SESSIONS.get()
        if mapping is None:
            return None
        return mapping.get((self._session_key, slot))

    def _ctx_set(self, slot: str, value) -> None:
        mapping = _REPO_SESSIONS.get()
        new = dict(mapping) if mapping else {}
        if value is None:
            new.pop((self._session_key, slot), None)
        else:
            new[(self._session_key, slot)] = value
        _REPO_SESSIONS.set(new)

    @property
    def _db(self) -> Optional[Session]:
        self._ensure_session_vars()
        return self._ctx_get("db")

    @_db.setter
    def _db(self, value: Optional[Session]) -> None:
        self._ensure_session_vars()
        self._ctx_set("db", value)

    @property
    def _db_generator(self):
        self._ensure_session_vars()
        return self._ctx_get("gen")

    @_db_generator.setter
    def _db_generator(self, value) -> None:
        self._ensure_session_vars()
        self._ctx_set("gen", value)

    def _open_session(self):
        """Open a new database session. Called lazily on first .db access."""
        self._db_generator = get_db()
        self._db = next(self._db_generator)

    @property
    def db(self) -> Session:
        """Get the database session, opening one lazily if needed.

        Checks the circuit breaker before returning the session. If the
        circuit is open (DB has been failing), raises OperationalError
        immediately instead of waiting for a pool timeout.
        """
        if not db_circuit_breaker.allow_request():
            raise OperationalError(
                "Database circuit breaker is open — failing fast",
                params=None,
                orig=None,
            )

        if self._db is None:
            self._open_session()

        # Rollback any failed transaction to reset session state
        try:
            if not self._db.is_active:
                self._db.rollback()
        except Exception as e:
            # Rollback failed — connection is likely severed (e.g. Neon idle timeout).
            # Create a fresh session instead of returning a broken one.
            logger.warning(
                f"Session recovery rollback failed, creating new session: {e}"
            )
            db_circuit_breaker.record_failure()
            try:
                self._db.close()
            except Exception as close_err:
                logger.warning(f"Failed to close broken session: {close_err}")
            self._open_session()
        return self._db

    def commit(self):
        """Commit the current transaction."""
        if self._db is None:
            return
        try:
            self._db.commit()
            db_circuit_breaker.record_success()
        except Exception as e:
            logger.warning(f"Error during commit: {e}")
            db_circuit_breaker.record_failure()
            self._db.rollback()
            raise

    def rollback(self):
        """Rollback the current transaction."""
        if self._db is None:
            return
        try:
            self._db.rollback()
        except Exception as e:
            logger.warning(f"Error during rollback: {e}")

    def commit_and_refresh(self, obj):
        """Commit, reload *obj*'s server-set fields, and END the read
        transaction the refresh opens (#907).

        `session.refresh()` emits a SELECT, which begins a fresh transaction
        after the preceding commit; without ending it the connection sits
        idle-in-transaction until GC returns it — universal-wrapper scale
        through `BaseService.track_execution`, and an outage risk under L.0's
        `max_overflow=0` pool. This is the one primitive for the
        add → commit → refresh pattern; use it wherever a caller needs the
        persisted row's generated fields back.
        """
        self.commit()
        if self._db is not None:
            self._db.refresh(obj)
        self.end_read_transaction()

    def end_read_transaction(self):
        """
        End a read-only transaction by committing (releases locks).

        Call this after read-only operations to prevent "idle in transaction"
        connections. In SQLAlchemy, even SELECT queries start a transaction
        that must be ended.

        If both commit and rollback fail (e.g. dead SSL connection), replaces
        the session entirely so the next operation starts clean.

        No-op if the session was never opened (lazy initialization).
        """
        if self._db is None:
            return
        try:
            self._db.commit()
        except Exception as commit_err:
            # If commit fails on a read-only transaction, rollback
            logger.debug(f"Read transaction commit failed, rolling back: {commit_err}")
            try:
                self._db.rollback()
            except Exception as rollback_err:
                # Both commit and rollback failed — connection is dead.
                # Replace the session entirely.
                logger.warning(
                    f"Session unrecoverable (commit: {commit_err}, "
                    f"rollback: {rollback_err}), creating fresh session"
                )
                try:
                    self._db.close()
                except Exception as close_err:
                    logger.warning(f"Failed to close dead session: {close_err}")
                self._open_session()

    def close(self):
        """
        Close the database session and return connection to pool.

        Call this when you're done with the repository to prevent
        connection pool exhaustion.

        No-op if the session was never opened (lazy initialization).
        """
        if self._db is None:
            return
        try:
            # Exhaust the generator to trigger the finally block
            # which closes the session
            try:
                next(self._db_generator)
            except StopIteration:
                pass  # Expected: generator already exhausted after first next()
        except Exception as e:
            logger.warning(f"Error closing database session: {e}")
        finally:
            # Also explicitly close just in case
            try:
                self._db.close()
            except Exception as e:
                # Suppressed: session.close() during cleanup is best-effort.
                # The session may already be closed or the pool invalidated.
                logger.debug(f"Suppressed error during session close: {e}")
            self._db = None
            self._db_generator = None

    def __del__(self):
        """Cleanup when repository is garbage collected."""
        try:
            self.close()
        except Exception:
            # Suppressed intentionally: during garbage collection / interpreter shutdown,
            # logging infrastructure may already be torn down. Attempting to log here
            # could itself raise errors. The close() method already has its own logging.
            pass

    def __enter__(self):
        """Context manager entry."""
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit - ensures session is closed."""
        self.close()
        return False  # Don't suppress exceptions

    def use_session(self, session: Session):
        """Temporarily use a shared session for coordinated multi-repo transactions.

        This allows multiple repositories to operate on the same database
        session so their writes can be committed or rolled back atomically.

        The caller is responsible for committing or rolling back the shared session.

        Args:
            session: An existing SQLAlchemy Session to use instead of this repo's own.
        """
        self._db = session

    def detach_session(self):
        """Forget the current context's session WITHOUT closing it.

        Used at task/thread boundaries. A coroutine spawned via
        ``asyncio.create_task`` or run via ``asyncio.to_thread`` copies the
        parent's context, inheriting the parent's Session reference. Calling this
        at the top of the spawned unit makes its next ``.db`` access open a FRESH,
        task-local session instead of sharing — and racing — the parent's. The
        inherited Session object is left intact for its owner (the parent) to
        commit and close.
        """
        self._db = None
        self._db_generator = None

    def _apply_tenant_filter(self, query, model_class, chat_settings_id: TenantScope):
        """Apply the tenant filter, fail-closed (F.1/#841).

        A tenant id filters; the explicit SYSTEM_SCOPE marker widens
        deliberately; None/empty raises — absent context never widens a
        query. This is the enforcing chokepoint: methods that query through
        it need no guard of their own.
        """
        require_tenant_context(chat_settings_id, where="_apply_tenant_filter")
        if chat_settings_id:
            query = query.filter(model_class.chat_settings_id == chat_settings_id)
        return query

    def _tenant_query(self, model_class, chat_settings_id: TenantScope):
        """Start a query with fail-closed tenant filtering applied.

        Context is validated BEFORE the session is touched, so a refused
        call never checks out a connection.
        """
        require_tenant_context(chat_settings_id, where="_tenant_query")
        query = self.db.query(model_class)
        return self._apply_tenant_filter(query, model_class, chat_settings_id)

    @staticmethod
    def _owned_or_null(model_class, chat_settings_id: TenantScope):
        """The owned-OR-NULL ownership rule as a SQL clause — the twin of
        :func:`write_allowed`, for writes that must stay ONE statement.

        A fetch-then-mutate path uses ``_get_for_write``; a conditional UPDATE
        whose atomicity is the point (``QueueRepository.transition``) cannot
        pre-read without reopening the race it exists to close, so it carries
        the ownership rule in its own WHERE clause instead.

        Two expressions of one rule is a fork risk, so they are defined as
        twins and pinned by a test that runs both over the same inputs
        (``test_the_sql_clause_and_the_python_predicate_agree``). A
        Returns a tuple of clauses to splat into ``.filter(*...)`` — EMPTY for
        a SYSTEM_SCOPE caller, meaning "add no restriction", which is the same
        permission ``write_allowed`` grants it. A tuple rather than an
        optional clause so no caller has to branch on a sentinel.
        """
        if not chat_settings_id:
            return ()
        return (
            or_(
                model_class.chat_settings_id.is_(None),
                model_class.chat_settings_id == chat_settings_id,
            ),
        )

    def _get_for_write(self, model_class, row_id, chat_settings_id: TenantScope):
        """Resolve a row for a tenant-scoped mutation, or None if the caller
        may not write it.

        Fetches by IDENTITY — deliberately system-scoped, and the SYSTEM_SCOPE
        marker stays visible so this sanctioned cross-tenant read keeps its
        entry in the #841 inventory.

        Identity-first rather than a filtered read, even though
        :meth:`_owned_or_null` could express the same rule in SQL: a filter
        returns None for a foreign row and for a missing one alike, so the
        method could not tell them apart and the refusal would go UNLOGGED.
        Fetch-then-check is what makes a cross-tenant write attempt visible;
        :meth:`_owned_or_null` documents the opposite trade.

        Then applies :func:`write_allowed`. A row owned by another tenant
        returns None — the mutator no-ops rather than raising, matching the
        not-found path a caller already handles — and is logged. The legacy
        NULL-owned fallback is logged too, so the pre-#412 path stays
        observable rather than merely permitted.
        """
        where = f"{model_class.__name__}._get_for_write"
        require_tenant_context(chat_settings_id, where=where)
        row = (
            self._tenant_query(model_class, SYSTEM_SCOPE)
            .filter(model_class.id == row_id)
            .first()
        )
        if row is None:
            self.end_read_transaction()
            return None
        if not write_allowed(row.chat_settings_id, chat_settings_id):
            logger.warning(
                "%s: refused cross-tenant write to %s (owner=%s, caller tenant=%s)",
                where,
                row_id,
                row.chat_settings_id,
                chat_settings_id,
            )
            return None
        if chat_settings_id and row.chat_settings_id is None:
            logger.warning(
                "%s: mutating legacy NULL-owned row %s under tenant %s "
                "(pre-#412 ownership backfill fallback)",
                where,
                row_id,
                chat_settings_id,
            )
        # Closes the read exactly as the by-id getters do (#908). The
        # MediaRepository original reached this through ``self.get_by_id``;
        # the generic form queries directly, so the read-close is explicit
        # rather than inherited. It runs AFTER the ownership check, not
        # before: reading ``row.chat_settings_id`` past a commit is free only
        # because ``expire_on_commit=False`` is set in src/config/database.py,
        # and this method should not depend on that from another file.
        self.end_read_transaction()
        return row

    @staticmethod
    def check_connection():
        """
        Verify database connectivity by executing a simple query.

        Used by HealthCheckService to test the database connection
        without violating the service/repository layer boundary.

        Raises:
            Exception: If database is unreachable or query fails
        """
        from sqlalchemy import text

        db = next(get_db())
        try:
            db.execute(text("SELECT 1"))
        finally:
            db.close()
