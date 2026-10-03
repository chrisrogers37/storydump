"""`identity.upsert_google_identity`'s sign-up gate (092, `07` §35), against a
scripted executor: a subject seen before signs in untouched, a NEW one creates
its user only when `fn_signup_admitted` admits its verified email, and
`signup_open` skips the ask. The door's own answers are
`tests/scripts/test_signup_gate.py`'s, as `svc_ingress` on the replayed schema.
"""

from __future__ import annotations

import pytest

from src.services.target import identity


class _Scripted:
    """Answers each `execute` from a queue of results, recording statements.
    A result is the first row (`first()`) and, for a scalar read, its value."""

    def __init__(self, *results):
        self.results, self.statements = list(results), []

    async def execute(self, statement, params=None):
        self.statements.append((str(statement), params))
        answer = self.results.pop(0)

        class _R:
            def first(self_inner):
                return answer

            def scalar(self_inner):
                return answer

            def scalar_one(self_inner):
                return answer

        return _R()

    def sql(self):
        return [s for s, _ in self.statements]


LOCK, NOT_SEEN = None, None


def _new_user_script(*, admitted=None):
    """lock, identity lookup (none), [door], collision check (none), the
    users insert (its id), the identity insert."""
    door = () if admitted is None else (admitted,)
    return (LOCK, NOT_SEEN, *door, None, "user-new", None)


class TestTheSignupGate:
    async def test_a_returning_subject_signs_in_without_asking_the_door(self):
        ex = _Scripted(LOCK, ("user-1", "p@example.com"), None)

        user = await identity.upsert_google_identity(
            ex, sub="sub-1", email="p@example.com", display_name="P"
        )

        assert user == "user-1"
        assert not any("fn_signup_admitted" in s for s in ex.sql())
        assert not any("INSERT INTO users" in s for s in ex.sql())

    async def test_an_admitted_email_creates_the_user(self):
        ex = _Scripted(*_new_user_script(admitted=True))

        user = await identity.upsert_google_identity(
            ex, sub="sub-2", email="new@example.com", display_name=None
        )

        assert user == "user-new"
        door = [p for s, p in ex.statements if "fn_signup_admitted" in s]
        assert door == [{"e": "new@example.com"}]
        sql = ex.sql()
        assert sql.index(next(s for s in sql if "fn_signup_admitted" in s)) < sql.index(
            next(s for s in sql if "INSERT INTO users" in s)
        ), "the door is asked before the user exists"

    async def test_an_email_nobody_admitted_creates_nothing(self):
        ex = _Scripted(LOCK, NOT_SEEN, False)

        with pytest.raises(identity.SignupNotAdmitted):
            await identity.upsert_google_identity(
                ex, sub="sub-3", email="stranger@example.com", display_name=None
            )

        assert not any("INSERT INTO" in s for s in ex.sql())
        assert not any("primary_email = :e" in s for s in ex.sql()), (
            "refused before the collision check: no oracle for who has an account"
        )

    async def test_a_new_subject_without_a_verified_email_is_refused(self):
        ex = _Scripted(LOCK, NOT_SEEN)

        with pytest.raises(identity.SignupNotAdmitted):
            await identity.upsert_google_identity(
                ex, sub="sub-4", email=None, display_name=None
            )

        assert not any("INSERT INTO" in s for s in ex.sql())

    async def test_the_gate_is_the_default(self):
        ex = _Scripted(LOCK, NOT_SEEN, False)
        with pytest.raises(identity.SignupNotAdmitted):
            await identity.upsert_google_identity(
                ex, sub="sub-5", email="x@example.com", display_name=None
            )

    @pytest.mark.parametrize("email", ["open@example.com", None])
    async def test_open_signup_never_asks(self, email):
        script = _new_user_script()
        if email is None:  # no claim, so no collision check either
            script = (LOCK, NOT_SEEN, "user-new", None)
        ex = _Scripted(*script)

        user = await identity.upsert_google_identity(
            ex, sub="sub-6", email=email, display_name=None, signup_open=True
        )

        assert user == "user-new"
        assert not any("fn_signup_admitted" in s for s in ex.sql())
        assert any("INSERT INTO users" in s for s in ex.sql())
