"""The target schema's shared column vocabulary.

Every model module needs these, so they live beside the base rather than
inside any one model module. Leaving them in `identity_and_tenancy` would make
every other model module either import a private name across modules or
re-derive them, and re-deriving `TZ` in particular is the specific mistake it
exists to prevent.
"""

from sqlalchemy import TIMESTAMP, Column, ForeignKey, text
from sqlalchemy.dialects.postgresql import UUID

#: ``TIMESTAMPTZ``, spelled ONCE deliberately. The parity gate
#: compares `information_schema` data types, and a naive `DateTime` renders
#: ``timestamp without time zone`` — so getting this wrong is a silent
#: divergence on every timestamp column in the schema rather than a loud one on
#: the first. Never use a naive `DateTime` here.
TZ = TIMESTAMP(timezone=True)

#: The server-side clock. `trg_touch_updated_at` owns
#: ``updated_at`` after insert.
NOW = text("now()")

#: The PK default the declarative tables use on id columns. PostgreSQL 15 provides
#: ``gen_random_uuid()`` in core, so no extension is required and they declare
#: none.
GEN_UUID = text("gen_random_uuid()")


def pk():
    """The target schema's standard surrogate primary key.

    A function rather than a module-level `Column`, and that is required rather
    than stylistic: a `Column` instance cannot be attached to two tables.
    """
    return Column(UUID(as_uuid=True), primary_key=True, server_default=GEN_UUID)


def timestamps():
    """The ``created_at``/``updated_at`` pair, unpacked into two attributes."""
    return (
        Column("created_at", TZ, nullable=False, server_default=NOW),
        Column("updated_at", TZ, nullable=False, server_default=NOW),
    )


def fk(target, ondelete, **kwargs):
    """A UUID foreign-key column.

    Written as a one-liner because the two facts a reviewer checks against the
    SQL — what it references and what happens on delete — are the two
    arguments, rather than being buried mid-block. The parity gate compares
    foreign keys by their rendered definition, so a wrong `ondelete` here is a
    red test and not a latent difference.
    """
    return Column(UUID(as_uuid=True), ForeignKey(target, ondelete=ondelete), **kwargs)
