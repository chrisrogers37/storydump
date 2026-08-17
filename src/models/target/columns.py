"""The target schema's shared column vocabulary.

Every F.2 increment's models need these, so they live beside the base rather
than inside whichever module happened to land first. The alternative — leaving
them in `identity_and_tenancy` — makes the next increment either import a
private name across increments or re-derive them, and re-deriving `TZ` in
particular is the specific mistake it exists to prevent.

Measured against the remaining advertised stream at the time of writing: of the
19 tables still to land, 19 use `TIMESTAMPTZ`, 19 carry `created_at`, 15 carry
`updated_at` and 12 have `gen_random_uuid()` primary keys. There is no
increment that wants none of this.
"""

from sqlalchemy import TIMESTAMP, Column, ForeignKey, text
from sqlalchemy.dialects.postgresql import UUID

#: ``TIMESTAMPTZ`` in the plan's SQL, spelled ONCE deliberately. The parity gate
#: compares `information_schema` data types, and a naive `DateTime` renders
#: ``timestamp without time zone`` — so getting this wrong is a silent
#: divergence on every timestamp column in the schema rather than a loud one on
#: the first. The legacy models use naive `DateTime` at 19 sites; do not copy
#: them here.
TZ = TIMESTAMP(timezone=True)

#: The server-side clock. `trg_touch_updated_at` (migration 052) owns
#: ``updated_at`` after insert.
NOW = text("now()")

#: The PK default the plan prints on every id column. PG15 ships
#: ``gen_random_uuid()`` in core, so no extension is required and 02/07 declare
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
