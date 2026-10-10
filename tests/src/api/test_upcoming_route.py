"""`GET /workspaces/{ws}/upcoming` — the adapter: the member gate, the range's
checks, and what reaches the read.

The read's own logic is `tests/src/services/target/test_upcoming.py`; that its
slots are the clock's, against a database, is
`tests/scripts/test_upcoming_gate.py`.
"""

from __future__ import annotations

from datetime import date

import pytest

from src.exceptions.tenancy import TenantResolutionError
from src.services.target import upcoming, vocabulary
from tests.src.api.conftest import PRINCIPAL, WS

URL = f"/api/v1/workspaces/{WS}/upcoming"

#: A November drawn in whole weeks, Monday first: six of them.
RANGE = {"from": "2026-10-26", "to": "2026-12-07"}

COMING = {
    "planned": [{"id": "i-1", "state": "scheduled", "day": "2026-11-03"}],
    "planned_truncated": False,
    "predicted": [{"kind": "predicted", "day": "2026-11-03"}],
    "predicted_truncated": True,
}


@pytest.fixture
def read(monkeypatch):
    """The read, patched: the calls it got, each answered with `COMING`."""
    calls = []

    async def fake(session, **kwargs):
        calls.append(kwargs)
        return dict(COMING)

    monkeypatch.setattr(upcoming, "upcoming", fake)
    return calls


class TestUpcoming:
    def test_a_member_gets_the_range_they_asked_for(
        self, client, signed_in, tenant, read
    ):
        resp = client.get(URL, params=RANGE)
        assert resp.status_code == 200
        assert resp.json() == {**RANGE, **COMING}
        assert read == [
            {
                "workspace_id": WS,
                "from_date": date(2026, 10, 26),
                "to_date": date(2026, 12, 7),
            }
        ]
        assert tenant == [
            ("uow", WS, PRINCIPAL.user_id),
            ("gate", WS, PRINCIPAL.user_id, "member"),
        ]

    def test_a_non_member_gets_the_house_404_and_nothing_is_read(
        self, client, signed_in, tenant, read
    ):
        tenant.refuse = TenantResolutionError("not_a_member")
        resp = client.get(URL, params=RANGE)
        assert resp.status_code == 404
        assert resp.json() == {"detail": "not found"}
        assert read == []

    def test_no_session_is_401(self, client, read):
        assert client.get(URL, params=RANGE).status_code == 401
        assert read == []

    @pytest.mark.parametrize(
        "params",
        [
            {},
            {"from": "2026-10-26"},
            {"to": "2026-12-07"},
            {"from": "next week", "to": "2026-12-07"},
            {"from": "2026-10-26", "to": "2026-10-26"},
            {"from": "2026-10-26", "to": "2026-10-25"},
            {"from": "2026-10-26", "to": "2026-12-11"},
        ],
        ids=[
            "neither end",
            "no end",
            "no start",
            "not a date",
            "an empty range",
            "an end before the start",
            "a day over the maximum",
        ],
    )
    def test_a_range_it_cannot_serve_is_a_422_before_any_seam(
        self, client, signed_in, tenant, read, params
    ):
        assert client.get(URL, params=params).status_code == 422
        assert tenant == [] and read == []

    def test_the_widest_range_is_served(self, client, signed_in, tenant, read):
        assert vocabulary.RANGE_MAX_DAYS == 45
        resp = client.get(URL, params={"from": "2026-10-26", "to": "2026-12-10"})
        assert resp.status_code == 200
        assert (read[0]["to_date"] - read[0]["from_date"]).days == 45
