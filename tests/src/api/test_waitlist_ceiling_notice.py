"""The notice a full waitlist ceiling gives (`routes/public.py`): at most one
each `CEILING_NOTICE_SECONDS`, counting the refusals in between."""

from __future__ import annotations

from src.api.routes import public


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _notice(refused: int) -> str:
    return (
        f"Waitlist signups reached the ceiling of {public.WAITLIST_ACCEPTED_LIMIT}"
        f" a minute: {refused} turned away as busy since the last notice."
    )


def test_the_first_refusal_gives_a_notice_then_the_window_holds_the_rest():
    clock = _Clock()
    notice = public.CeilingNotice(clock)
    assert notice.refused() == _notice(1)
    clock.now += public.CEILING_NOTICE_SECONDS - 1
    assert [notice.refused() for _ in range(3)] == [None, None, None]


def test_past_the_window_the_next_notice_counts_what_the_window_held():
    clock = _Clock()
    notice = public.CeilingNotice(clock)
    notice.refused()
    notice.refused()
    notice.refused()
    clock.now += public.CEILING_NOTICE_SECONDS
    assert notice.refused() == _notice(3)
    assert notice.refused() is None
