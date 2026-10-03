"""`delivery_health.outbox_failures`: the read behind `/health/delivery` (#1482).

The statements are the doors 091 creates, called with the hour as an
explicitly typed parameter (asyncpg infers nothing from a bare `:w`). The
aggregation is what the monitor reads: the alerting count sums only the rows
that ended `failed` or sit `ambiguous`, and a code is a string key with `none`
for no answer. What the doors answer, the window's clamp included, is the
gate's (`tests/scripts/test_outbox_failure_record_gate.py`); here the rows are
scripted."""

from __future__ import annotations

from src.services.target import delivery_health


class _Doors:
    """Answers 091's two doors from scripted rows and refuses anything else."""

    def __init__(self, failures=(), sent=0):
        self.failures = [
            {"failure_class": c, "error_code": code, "rows": n, "alerting": a}
            for c, code, n, a in failures
        ]
        self.sent = sent
        self.statements = []

    async def execute(self, stmt, params=None):
        sql = " ".join(str(stmt).split())
        self.statements.append((sql, dict(params or {})))
        doors = self

        class _Result:
            def mappings(self_inner):
                return self_inner

            def all(self_inner):
                return doors.failures

            def scalar_one(self_inner):
                return doors.sent

        if "fn_health_outbox_failures" in sql or "fn_health_outbox_sent" in sql:
            return _Result()
        raise AssertionError(f"not a door: {sql}")


class TestItReadsThroughTheDoors:
    async def test_both_doors_with_the_window_typed(self):
        doors = _Doors()
        await delivery_health.outbox_failures(doors)
        (failures_sql, failures_params), (sent_sql, sent_params) = doors.statements
        assert "FROM fn_health_outbox_failures(CAST(:w AS integer))" in failures_sql
        assert sent_sql == "SELECT fn_health_outbox_sent(CAST(:w AS integer)) AS sent"
        assert failures_params == sent_params == {"w": 3600}


class TestWhatItReturns:
    ROWS = [
        ("destination_gone", 403, 5, 5),
        ("destination_gone", 400, 1, 1),
        ("rate_limited", 429, 9, 0),
        ("ambiguous", None, 2, 1),
    ]

    async def test_classes_codes_and_the_alerting_count(self):
        got = await delivery_health.outbox_failures(_Doors(self.ROWS, sent=40))
        assert got == {
            "window_seconds": 3600,
            "sent_in_window": 40,
            "failed_or_ambiguous": 7,
            "by_class": {
                "destination_gone": {
                    "rows": 6,
                    "alerting": 6,
                    "codes": {"403": 5, "400": 1},
                },
                "rate_limited": {"rows": 9, "alerting": 0, "codes": {"429": 9}},
                "ambiguous": {"rows": 2, "alerting": 1, "codes": {"none": 2}},
            },
        }

    async def test_a_429_hour_alerts_on_nothing(self):
        """A deferral is context: nine rate-limited rows are an alerting count
        of zero, and they stay visible in `by_class`."""
        got = await delivery_health.outbox_failures(
            _Doors([("rate_limited", 429, 9, 0)], sent=12)
        )
        assert got["failed_or_ambiguous"] == 0
        assert got["by_class"]["rate_limited"]["rows"] == 9

    async def test_an_empty_hour_is_zeros_and_says_how_much_was_sent(self):
        got = await delivery_health.outbox_failures(_Doors(sent=0))
        assert got == {
            "window_seconds": 3600,
            "sent_in_window": 0,
            "failed_or_ambiguous": 0,
            "by_class": {},
        }
