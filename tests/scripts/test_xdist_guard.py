"""#809 — this directory refuses to run under pytest-xdist.

`tests/scripts/` serializes every session on `SUITE_CLUSTER_LOCK_KEY`, because
the seven `svc_*` roles are cluster-scoped spec names that cannot be namespaced
per session (#768). Under `-n` each worker is a separate process with its own
session, so workers 2..N queue on a lock the first holds for the whole run —
and the wait loop reports by `print` from session-scoped fixture setup, which
pytest captures. The developer sees twenty silent minutes and then a
`RuntimeError`.

WHAT IS ASSERTED HERE IS THE REFUSAL, NOT SAFETY. Nothing in #809 makes this
directory parallel-safe; the guard converts a slow silent failure into a fast
explanatory one. A reader arriving at these tests should not conclude the suite
can be parallelized.

These exercise the predicate without pytest-xdist installed — it is deliberately
not a dependency of this project, and a guard that needed the plugin it refuses
could never run in CI. The end-to-end behaviour (a real `-n auto` run reporting
a scoped collection error while the rest of the suite still passes) is proven by
running it for real; that evidence is cited on the PR, not re-derived here.
"""

import pytest

from tests.scripts.conftest import pytest_configure


class _Config:
    """Stands in for pytest's `Config`.

    The guard's whole contract is one attribute: pytest-xdist sets
    ``workerinput`` on the config of a WORKER process and never on the
    controller's, which is how `is_xdist_worker` itself decides. Nothing else
    about `Config` is read, so a stub is faithful here rather than convenient.
    """


def _worker_config():
    config = _Config()
    config.workerinput = {"workerid": "gw0", "workercount": 4}
    return config


class TestTheDirectoryRefusesXdist:
    def test_a_worker_process_is_refused_by_name(self):
        """Bound to the SPECIFIC refusal, not to any raise.

        A bare `UsageError` would also be raised by an unrelated future
        misconfiguration, so the message has to carry the two things that make
        it actionable: WHY (the cluster-wide lock, named) and WHAT TO DO
        INSTEAD. The second is what stops the guard merely relocating the
        confusion the guides created — a developer told "no" with no route
        forward reaches for `-p no:cacheprovider` and similar guesswork.
        """
        with pytest.raises(pytest.UsageError) as caught:
            pytest_configure(_worker_config())

        message = str(caught.value)
        assert "SUITE_CLUSTER_LOCK_KEY" in message
        assert "pytest-xdist" in message
        assert "--ignore=tests/scripts" in message, (
            "the refusal must name the way to keep parallelism for the rest of"
            f" the suite; got: {message}"
        )

    def test_a_controller_or_serial_run_is_untouched(self):
        """The positive control, and it is the half that can go vacuous.

        A guard that raised unconditionally would satisfy the test above
        perfectly while breaking every serial run — including CI, which is the
        one consumer #809 established is currently unaffected. This is the
        assertion that says the discriminator is `workerinput` rather than the
        guard's mere existence.
        """
        assert pytest_configure(_Config()) is None

    def test_an_unrelated_attribute_does_not_trip_it(self):
        """`workerinput` specifically — not "the config has extra attributes".

        A real `Config` carries a large surface of its own; a predicate that
        keyed on anything looser would refuse serial runs the moment pytest or
        a plugin added a field.
        """
        config = _Config()
        config.workerid = "gw0"  # near-miss name, deliberately not the one
        config.option = object()

        assert pytest_configure(config) is None
