"""#1090 D6 — the two facts the "not yet" verdict rests on, pinned.

D6 asks whether an item needing a human decision can be resolved. The answer is
no, and the reason it is no matters more than the fact: `resolve_review` and
`clear_quarantine` are unbuilt AND unreachable, so building their executors now
would produce commands with nothing to act on — which is the same
advertise-a-capability-nothing-performs defect the epic exists to remove, built
fresh.

**The two are unreachable for DIFFERENT reasons at different layers, and the
difference decides what would reopen each.** Neither is "the estate is empty":

* `clear_quarantine` — `provider_quarantine` has **no writer in the codebase at
  all**. No column, no seam, no configuration reopens it; only new code does.
  This is the strongest form of unreachable.
* `resolve_review` — reachability is a chain, and the **first** gate is NOT the
  load-bearing one. `approve` refuses under `manual_mode`, but
  `api_publishing_enabled` is a `settings_change` key and `settings_change` is
  BUILT, so any admin can flip it: on its own that gate is configuration, not
  structure. The structural gate is one layer deeper — with the flag on, the
  `publish_pipeline` job PARKS, because production composes `media_fetch=None`
  (`worker.py`, W5b unbuilt), and `reconcile_ambiguous` — the other
  `review_required` producer — parks on `poll=None` (W5a). Absent code, and a
  constant in the composition root.

**Superseded in part on 2026-09-10 (#1276, the publish leg):** production now
composes `media_fetch`, `meta`, `transit` AND `poll`, so both producers are
live and `review_required` is reachable. The structural test below pins the
new reality (parked under the DEFAULT seam set only; live under the production
composition); D6 is reopened on #1124.

Both tests below assert the structural gate. The `approve` gate is asserted too,
because it is the first door and its removal is worth noticing, but it is
labelled as the weak one so nobody reads it as the whole argument.

That verdict is only true while these two facts hold. **These tests exist to
FAIL when they stop holding**, and each failure message says what to do about
it. A test that fails when someone does legitimate work is the right shape here:
the work in question is exactly the work that reopens D6.

Neither test asserts anything about the floor itself — the ROLE refusal is
pinned by `test_commands.py::test_operator_floor_refuses_a_user_principal`, and
nothing in this change moves it.
"""

from __future__ import annotations

import asyncio
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


class _NullSession:
    """Enough session for the executor's own statements; the seams are stubbed.

    The executor runs `apply_gucs` and the notify branch against this; neither
    is the subject here, and a real database would make a reachability test
    depend on data.
    """

    async def execute(self, *a, **k):
        class _R:
            def mappings(self):
                return self

            def first(self):
                return None

            def all(self):
                return []

            @property
            def rowcount(self):
                return 0

        return _R()

    async def commit(self):
        return None


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


def test_the_review_producers_park_only_under_the_default_seam_set():
    """The STRUCTURAL half — RE-POINTED AGAIN, this time because the fact it
    pinned stopped holding on purpose (#1276, the publish leg, 2026-09-10).

    Until #1276 both `review_required` producers sat behind worker seams that
    production composed as None: `publish_pipeline` parked on
    `media_fetch=None` (W5b unbuilt) and `reconcile_ambiguous`'s ladder half
    skipped on `poll=None` (W5a). That was the load-bearing fact behind #1090
    D6's "not yet" — no intent could reach `review_required`, so the
    unbuilt operator floor (`resolve_review`, #1124) had nothing to act on.

    #1276 is exactly the work this test existed to notice: production now
    composes the Drive-backed fetch, the Instagram Graph adapter, the transit
    store AND the reconciler's poll, so BOTH producers are live where Drive
    and `CLOUDINARY_*` are configured. D6 is therefore reopened — recorded on
    #1124 and the follow-up it names — and the interim is honest rather than
    hidden: `post_intents.last_error` says why an intent parked, and the
    `06` §5 customer notice still fires.

    What still holds, and is pinned here so it cannot drift silently:

    * under the DEFAULT seam set (no Drive adapter, no transit store) the
      pipeline kind is `Parked` and the ladder half is skipped — a bare
      composition still cannot park anything;
    * under the PRODUCTION composition (a Drive adapter + `CLOUDINARY_*`)
      both producers are live — asserted so that a later "tidy-up" that
      re-parks one of them by accident is noticed as loudly as the un-park
      was.
    """
    from src.services.target.work_loop import Parked, WorkerDeps, build_registry
    from src.worker import WorkerConfig, compose

    registry = build_registry(WorkerDeps())

    assert isinstance(registry["publish_pipeline"], Parked), (
        "publish_pipeline has an executor under the DEFAULT seam set — a bare"
        " composition could now park an intent into `review_required`"
    )

    # The parking path, driven under the default seam set: a ladder-due row is
    # the only input that can reach `_park_review_required`, and the branch
    # that would consume it is skipped without a poll.
    from src.services.target import reconciler as _rec

    reached: list[str] = []

    async def _sweep(session, *, limit, notify_after_seconds):
        return [{"intent_id": "i", "workspace_id": "w", "reason": "ladder_due"}]

    async def _reconcile(session, **kw):
        reached.append("reconcile_intent")
        return "review_required"

    original = (_rec.sweep_due, _rec.reconcile_intent)
    _rec.sweep_due, _rec.reconcile_intent = _sweep, _reconcile
    try:
        asyncio.run(
            registry["reconcile_ambiguous"](
                _NullSession(),
                {"id": "j", "kind": "reconcile_ambiguous", "workspace_id": None},
            )
        )
    finally:
        _rec.sweep_due, _rec.reconcile_intent = original

    assert not reached, (
        "reconcile_ambiguous reached `reconcile_intent` under the DEFAULT seam"
        " set — the ladder half no longer waits for a poll"
    )

    # The production composition: both producers live. This is the fact that
    # reopened D6; it is pinned so it cannot quietly flip back.
    env = {
        "CLOUDINARY_CLOUD_NAME": "c",
        "CLOUDINARY_API_KEY": "k",
        "CLOUDINARY_API_SECRET": "s",
    }
    app = compose(engine=object(), config=WorkerConfig(), env=env, drive=object())
    assert not isinstance(app.registry["publish_pipeline"], Parked), (
        "publish_pipeline parks under the production composition — the publish"
        " leg (#1276) regressed; see #1220 step 3"
    )
    assert callable(app.deps.poll), (
        "production composes poll=None again — the reconciler's ladder half is"
        " dead and a lost publish response parks for a human every time (#1276)"
    )


def test_the_publish_pipeline_has_exactly_one_producer_and_it_is_manual_mode_gated():
    """`review_required`'s only two producers (`reconciler`, `publish_pipeline`)
    both sit inside the publish pipeline, and the pipeline runs only from a
    `publish_pipeline` job. So the reachability of the whole parked-intent state
    reduces to who mints that job — and today that is `approve` alone, which
    refuses with `manual_mode` unless the workspace has `api_publishing_enabled`.

    In the manual-mode product #1090 measures, no intent can be parked.

    **This is the WEAK gate of the two** — `api_publishing_enabled` is a
    `settings_change` key and that command is built, so an admin can flip it
    without a deploy. It is pinned because it is the first door and its removal
    is worth knowing about, not because the D6 verdict rests on it. The verdict
    rests on the test above.
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
        " Recheck the review card's resolutions (`resolve_review`, 2026-09-12)."
    )

    source = (SERVICES / "target" / "command_executors.py").read_text()
    approve = source.split("async def approve(")[1].split("\nasync def ")[0]
    # Since 2026-09-12 the review card's `retry` resolution re-mints the job
    # for an intent that ALREADY passed `approve`'s gate, and re-checks the
    # flag itself — so the manual-mode gate holds at both doors.
    resolve = source.split("async def resolve_review(")[1].split("\nasync def ")[0]
    assert "manual_mode" in resolve, "the retry resolution must keep approve's gate"
    assert (
        len(mints.findall(source)) == 2
        and mints.search(approve)
        and mints.search(resolve)
    ), (
        "a publish_pipeline job is now minted outside `approve`/`resolve_review` — see above."
    )
    assert '"manual_mode"' in approve, (
        "`approve` no longer refuses in manual mode — the gate that makes the"
        " publish pipeline unreachable for a manual-mode workspace is gone."
        " Reopen #1090 D6 (#1124)."
    )
