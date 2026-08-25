"""The sync tier's transaction substrate — the counterpart to `unit_of_work`.

The target tier has two lanes. The ASYNC lane (`unit_of_work.py`) owns the
engine, the tenant-scoped transaction and the GUC helper for the Phase L
pipeline. The SYNC lane takes caller-supplied DB-API connections
(`tenant_resolution` set that convention and the X.3 writers follow it),
and until this module it had no substrate at all — so each writer grew its
own piece of one: a transaction precondition in the workspace door, a
savepoint in the identity door, four hand-written `SET LOCAL` statements in
one of them and one in another. Three private copies of a thing that is one
thing.

**The GUC NAMES ARE THE INVARIANT AND THEY LIVE ONCE, HERE.** `apply_gucs` in
`unit_of_work` calls itself "the ONE spelling of the security invariant" and
predicts this exact failure: hand-rolled copies are how a third one ships
wrong. It cannot be reused literally — it is `async`, it renders SQLAlchemy
`text()` clauses a DB-API cursor cannot take, and it binds `:v` where psycopg2
wants `%s` — so what is shared is `guc_pairs`, the name list and the skip-None
rule, which is the half that actually drifts. Adding a fifth GUC to the schema
must not be a change one lane gets and the other silently does not.

**`SELECT set_config(name, value, true)` rather than `SET LOCAL x = %s`**, and
that is portability rather than taste: `SET` cannot take a bind parameter, so
the `SET LOCAL` spelling works only because psycopg2 interpolates client-side
— a driver with server-side binding rejects it. `set_config(..., true)` IS
`SET LOCAL`, takes both arguments as real parameters, and makes the two lanes
literally the same statement.

**Transaction-local, never session-local.** The third argument is `is_local`,
and it is `true` everywhere for the reason `unit_of_work` states: the value
dies with the transaction, so the next task to borrow a pooled connection
cannot inherit another tenant's scope.
"""

import uuid
from contextlib import contextmanager
from typing import Optional

from src.exceptions.base import StorydumpError

#: The GUC vocabulary `02` §7's policies and `055`'s audit triggers read. The
#: tenant is mandatory; the rest are omitted when absent rather than set empty,
#: because the policies use `NULLIF(current_setting(...), '')` and an empty
#: string and an unset GUC must stay the same thing.
TENANT_GUC = "app.tenant_id"
ACTOR_KIND_GUC = "app.actor_kind"
ACTOR_USER_GUC = "app.actor_user_id"
CHANNEL_GUC = "app.channel"


class TransactionRequired(StorydumpError):
    """A door that needs a transaction was handed an autocommit connection.

    A substrate condition, deliberately NOT a domain refusal: it is a caller
    misusing the tier, not a tenant or an identity failing to resolve, and
    putting it in a domain vocabulary would teach an edge to render it as one.
    """


def guc_pairs(
    *,
    tenant_id: str,
    actor_kind: Optional[str] = None,
    actor_user_id: Optional[str] = None,
    channel: Optional[str] = None,
) -> tuple:
    """The (name, value) pairs to apply, Nones dropped. Driver-free on purpose
    — this is the vocabulary, not the mechanism, so both lanes can render it."""
    pairs = [(TENANT_GUC, tenant_id)]
    for name, value in (
        (ACTOR_KIND_GUC, actor_kind),
        (ACTOR_USER_GUC, actor_user_id),
        (CHANNEL_GUC, channel),
    ):
        if value is not None:
            pairs.append((name, value))
    return tuple(pairs)


def apply_gucs(cur, **kwargs) -> None:
    """`SET LOCAL` the tenancy/actor GUCs on a DB-API cursor."""
    for name, value in guc_pairs(**kwargs):
        cur.execute("SELECT set_config(%s, %s, true)", (name, str(value)))


def require_transaction(conn) -> None:
    """Refuse an autocommit connection, by name and before any write.

    Every door in this tier depends on the caller owning the commit — that is
    what lets sign-in's three legs compose into one transaction and what makes
    `SET LOCAL` and `SAVEPOINT` mean anything. Under autocommit each fails
    differently and none of them usefully: the GUCs would be discarded and the
    write refused by RLS with an unrelated message, a savepoint dies on a raw
    `25P01`, and a mint that should have been atomic with its identity just
    silently is not. One check, one message, before any of that.
    """
    if getattr(conn, "autocommit", False):
        raise TransactionRequired(
            "this door writes inside the caller's transaction: SET LOCAL and"
            " SAVEPOINT are transaction-scoped, and the mints it composes with"
            " must commit together or not at all"
        )


@contextmanager
def savepoint(conn):
    """A speculative write that leaves the caller's transaction usable.

    Released on success, rolled back to on any exception — so a door can raise
    a typed refusal from a caught integrity error without poisoning the
    transaction the caller still needs to render the page that says so.

    The name is unique per entry rather than fixed. Postgres permits duplicate
    savepoint names and `ROLLBACK TO` targets the most recent, so a fixed name
    is *correct* under nesting — but it makes the release ambiguous to a reader
    and to anything that later wants to release by name, and this is now a
    shared helper rather than one door's private detail.
    """
    name = f"sd_{uuid.uuid4().hex[:12]}"
    with conn.cursor() as cur:
        cur.execute(f"SAVEPOINT {name}")
        try:
            yield cur
        except BaseException:
            cur.execute(f"ROLLBACK TO SAVEPOINT {name}")
            raise
        else:
            cur.execute(f"RELEASE SAVEPOINT {name}")
