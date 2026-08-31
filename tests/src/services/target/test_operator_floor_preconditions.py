"""#1090 D6 — the two facts the "not yet" verdict rests on, pinned.

D6 asks whether an item needing a human decision can be resolved. The answer is
no, and the reason it is no matters more than the fact: `resolve_review` and
`clear_quarantine` are unbuilt AND unreachable, so building their executors now
would produce commands with nothing to act on — which is the same
advertise-a-capability-nothing-performs defect the epic exists to remove, built
fresh.

That verdict is only true while these two facts hold. **These tests exist to
FAIL when they stop holding**, and each failure message says what to do about
it. A test that fails when someone does legitimate work is the right shape here:
the work in question is exactly the work that reopens D6.

Neither test asserts anything about the floor itself — the ROLE refusal is
pinned by `test_commands.py::test_operator_floor_refuses_a_user_principal`, and
nothing in this change moves it.
"""

from __future__ import annotations

import re
from pathlib import Path

SERVICES = Path(__file__).resolve().parents[4] / "src" / "services"

#: A write, not a mention. `provider_quarantine` appears in comments and in the
#: ORM model; neither is a producer.
_WRITE = re.compile(
    r"(INSERT\s+INTO|UPDATE|DELETE\s+FROM)\s+provider_quarantine", re.IGNORECASE
)


def _service_sources() -> list[Path]:
    files = sorted(SERVICES.rglob("*.py"))
    assert files, f"no service sources under {SERVICES} — the path went stale"
    return files


def test_nothing_writes_provider_quarantine_so_there_is_nothing_to_clear():
    """`clear_quarantine`'s whole subject is a `provider_quarantine` row.

    `054` documents entry as "the adapter upserts the row" and the only reader
    is `fn_claim_job`'s deferral predicate — but no adapter does. A row cannot
    be cleared before anything creates one, so the command has no work.
    """
    writers = [
        f"{p.relative_to(SERVICES)}:{i}"
        for p in _service_sources()
        for i, line in enumerate(p.read_text().splitlines(), 1)
        if _WRITE.search(line)
    ]
    assert not writers, (
        "provider_quarantine now has a producer at "
        + ", ".join(writers)
        + " — a quarantine can now exist, so `clear_quarantine` has something to"
        " clear. Reopen #1090 D6 (#1124): the executor is now worth building, and the"
        " operator-principal question it was waiting behind is now load-bearing."
    )


def test_the_publish_pipeline_has_exactly_one_producer_and_it_is_manual_mode_gated():
    """`review_required`'s only two producers (`reconciler`, `publish_pipeline`)
    both sit inside the publish pipeline, and the pipeline runs only from a
    `publish_pipeline` job. So the reachability of the whole parked-intent state
    reduces to who mints that job — and today that is `approve` alone, which
    refuses with `manual_mode` unless the workspace has `api_publishing_enabled`.

    In the manual-mode product #1090 measures, no intent can be parked.
    """
    # The FILE, never the line: a line number pins where the producer sits
    # today, which every neighbouring edit moves, and says nothing about the
    # property. The property is that there is one producer and it is `approve`.
    mints = re.compile(r'kind\s*=\s*["\']publish_pipeline["\']')
    minters = sorted(
        str(p.relative_to(SERVICES))
        for p in _service_sources()
        if mints.search(p.read_text())
    )
    assert minters == ["target/command_executors.py"], (
        f"the set of publish_pipeline job producers is now {minters} —"
        " `review_required` may be reachable without api_publishing_enabled."
        " Reopen #1090 D6 (#1124) and recheck whether `resolve_review` now has parked"
        " intents to resolve."
    )

    source = (SERVICES / "target" / "command_executors.py").read_text()
    approve = source.split("async def approve(")[1].split("\nasync def ")[0]
    assert len(mints.findall(source)) == 1 and mints.search(approve), (
        "a publish_pipeline job is now minted outside `approve` — see above."
    )
    assert '"manual_mode"' in approve, (
        "`approve` no longer refuses in manual mode — the gate that makes the"
        " publish pipeline unreachable for a manual-mode workspace is gone."
        " Reopen #1090 D6 (#1124)."
    )
