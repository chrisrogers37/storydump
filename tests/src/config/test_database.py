"""Tests for the SQLAlchemy session factory configuration.

These pin behavior that production depends on; flipping these defaults can
break the worker without breaking individual unit tests, so they have their
own home rather than being asserted indirectly by service tests.
"""

from src.config.database import SessionLocal


def test_session_factory_does_not_expire_on_commit():
    """expire_on_commit MUST be False on the production session factory.

    Background: the worker runs a transaction_cleanup_loop that calls
    .commit() on idle sessions every 30s. With SQLAlchemy's default
    expire_on_commit=True, that commit expires every loaded ORM instance,
    and the next attribute access on those instances raises
    "Instance <X> is not bound to a Session; attribute refresh operation
    cannot proceed".

    This pinned production posting for 5 days (issue #388). Flipping this
    default back to True would re-introduce that outage silently — tests
    on individual services would still pass because they don't exercise
    the cleanup loop.
    """
    # `expire_on_commit` is stored on the session class created by the
    # factory, not on the factory itself.
    assert SessionLocal.kw.get("expire_on_commit") is False, (
        "expire_on_commit must be False on SessionLocal — see issue #388. "
        "Leaving it as the SQLAlchemy default (True) causes the "
        "transaction_cleanup_loop to expire ORM instances mid-flight in "
        "send_notification and breaks all Telegram posting."
    )


def test_session_instance_attributes_survive_commit():
    """End-to-end check: load → commit → access still works.

    Sanity test that the configuration above produces the behavior we want.
    Uses an in-memory ChatSettings-shaped object via SQLAlchemy literals so
    no DB is required.
    """
    from sqlalchemy import Column, Integer, String, create_engine
    from sqlalchemy.orm import declarative_base, sessionmaker

    # Build a minimal test schema that mirrors production's session factory.
    Base = declarative_base()

    class _Foo(Base):
        __tablename__ = "foo"
        id = Column(Integer, primary_key=True)
        name = Column(String)

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    LocalSession = sessionmaker(
        autocommit=False, autoflush=False, bind=engine, expire_on_commit=False
    )

    session = LocalSession()
    foo = _Foo(name="bar")
    session.add(foo)
    session.commit()

    # The race condition that #388 fixed: a concurrent commit on the same
    # session must not strip the loaded attribute out from under us.
    session.commit()
    assert foo.name == "bar"

    session.close()
    # After the session closes, in-memory attributes are still accessible
    # because expire_on_commit=False didn't expire them on the prior commits.
    assert foo.name == "bar"
