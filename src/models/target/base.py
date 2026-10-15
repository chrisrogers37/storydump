"""The target lineage's declarative base.

``TargetBase`` carries a ``MetaData`` of its own, in its own module, and only
the target model modules register against it.
"""

from sqlalchemy.orm import declarative_base

TargetBase = declarative_base()
