"""A gate-shaped file for ``tests/test_isolation_guard.py``'s subprocess runs.

It lives in ``tests/scripts/`` so those runs load the conftest chain the real
gate runs under, and it passes with no database, so the controls that RUN a
test can: the real gate (``tests/scripts/test_instagram_source_gate.py``, which
``GATE_TARGET`` there names) would run for real against whatever the control
had blocked.

Deliberately not named ``test_*``: the suite does not collect it, and only an
explicit path argument does.
"""


def test_the_control_target_collects_and_passes():
    assert True
