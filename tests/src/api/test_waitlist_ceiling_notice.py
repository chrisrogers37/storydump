"""The notice a full waitlist limit gives (`routes/public.py`): at most one
each `CEILING_NOTICE_SECONDS`, counting the refusals in between."""

from __future__ import annotations

from src.api.routes import public


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _notice(refused: int, since: str) -> str:
    return (
        "Waitlist signups hit the limit of 600 a minute that all visitors"
        f" share, and are being turned away as busy: {refused} on this server"
        f" {since}."
    )


def test_the_first_refusal_gives_a_notice_then_the_window_holds_the_rest():
    clock = _Clock()
    notice = public.CeilingNotice(clock)
    assert notice.refused(600) == _notice(1, "so far")
    clock.now += public.CEILING_NOTICE_SECONDS - 1
    assert [notice.refused(600) for _ in range(3)] == [None, None, None]


def test_past_the_window_the_next_notice_counts_what_the_window_held():
    clock = _Clock()
    notice = public.CeilingNotice(clock)
    for _ in range(3):
        notice.refused(600)
    clock.now += public.CEILING_NOTICE_SECONDS
    assert notice.refused(600) == _notice(3, "in the 10 minutes since its last notice")
    assert notice.refused(600) is None
