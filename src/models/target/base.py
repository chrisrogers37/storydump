"""The target lineage's declarative base.

Separate ``MetaData`` from the legacy ``Base`` by construction — that
separation is the whole mechanism, so it lives in its own module where nothing
can accidentally register a legacy model against it or vice versa.
"""

from sqlalchemy.orm import declarative_base

TargetBase = declarative_base()
