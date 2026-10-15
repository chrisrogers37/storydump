"""#1090 — the preconditions behind `resolve_review` and `clear_quarantine`, pinned.

`review_required` is the state of a story that needs a human decision, and
`resolve_review` acts on it; `clear_quarantine` acts on `provider_quarantine`.
The two are differently built and differently reachable:

* `resolve_review` — BUILT, on the member floor: the review card is the
  workspace's to resolve, with `retry`, `posted` or `cancel`. `failed` (a
  refund) is not offered; it stays the operator's.
* `clear_quarantine` — NOT built, on the operator floor, which no principal
  this tier can build satisfies (#1124). It has nothing to act on either:
  `provider_quarantine` has **no writer in the codebase at all**, so no
  column, no seam and no configuration creates a row; only new code does. An
  executor for it would advertise a capability nothing performs — the #1090
  defect.

**`review_required` is reachable through a chain, and the first gate is NOT
the load-bearing one.** Every approval that mints the `publish_pipeline` job
(`approve`, the review's `retry`, the serve step's automatic approval) goes
ahead only when the workspace has `api_publishing_enabled` — but that is a
`settings_change` key and `settings_change` is BUILT, so any admin can flip
it: on its own that gate is configuration, not structure. The structural gate
is the composition. Both `review_required` producers sit behind worker seams:
under the DEFAULT seam set `publish_pipeline` is `Parked` and
`reconcile_ambiguous`'s ladder half skips without a `poll`; under the
production composition (#1276) both are live wherever a Drive adapter and the
`CLOUDINARY_*` trio are configured, because `src/worker.py`'s `compose`
always builds the Instagram Graph adapter and its `poll`, the media fetch
from the Drive adapter, and the transit store from `CLOUDINARY_*`. So a
manual-mode workspace parks nothing, and a workspace that posts through the
API can park a story, which its members resolve.

**These tests exist to FAIL when a precondition stops holding**, and each
failure message says what that opens. The structural gate is asserted under
both compositions. The `approve` gate is asserted too, because it is the first
door and its removal is worth noticing, but it is labelled as the weak one so
nobody reads it as the whole argument.

None of the tests asserts anything about the floor itself — the operator
floor's ROLE refusal is pinned by
`test_commands.py::test_operator_floor_refuses_a_user_principal`.
"""

from __future__ import annotations

import ast
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
        "provider_quarantine has a producer at "
        + ", ".join(writers)
        + " — a quarantine can exist, so `clear_quarantine` has something to"
        " clear: its executor is worth building (#1090), and the operator"
        " principal it waits on (#1124) is load-bearing."
    )


def test_the_review_producers_park_only_under_the_default_seam_set():
    """The STRUCTURAL half (#1276, the publish leg).

    Both `review_required` producers sit behind worker seams:
    `publish_pipeline` parks unless `media_fetch`, `meta` and `transit` are
    composed, and `reconcile_ambiguous`'s ladder half skips without `poll`.
    Production composes the Drive-backed fetch, the Instagram Graph adapter,
    the transit store AND the reconciler's poll, so BOTH producers are live
    where Drive and `CLOUDINARY_*` are configured. A parked intent is not
    hidden: `post_intents.last_error` says why it parked, and the customer
    notice fires.

    Pinned here so it cannot drift silently:

    * under the DEFAULT seam set (no Drive adapter, no transit store) the
      pipeline kind is `Parked` and the ladder half is skipped — a bare
      composition cannot park anything;
    * under the PRODUCTION composition (a Drive adapter + `CLOUDINARY_*`)
      both producers are live — asserted so that a "tidy-up" that
      re-parks one of them by accident is noticed.
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

    # The production composition: both producers live, pinned so neither can
    # quietly park.
    env = {
        "CLOUDINARY_CLOUD_NAME": "c",
        "CLOUDINARY_API_KEY": "k",
        "CLOUDINARY_API_SECRET": "s",
    }
    app = compose(
        engine=object(), config=WorkerConfig(), env=env, adapters={"gdrive": object()}
    )
    assert not isinstance(app.registry["publish_pipeline"], Parked), (
        "publish_pipeline parks under the production composition — the publish"
        " leg (#1276) regressed; see #1220"
    )
    assert callable(app.deps.poll), (
        "production composes poll=None — the reconciler's ladder half is"
        " dead and a lost publish response parks for a human every time (#1276)"
    )


def test_the_publish_pipeline_has_exactly_one_producer_and_it_is_manual_mode_gated(
    monkeypatch,
):
    """`review_required`'s only two producers (`reconciler`, `publish_pipeline`)
    both sit inside the publish pipeline, and the pipeline runs only from a
    `publish_pipeline` job. So the reachability of the whole parked-intent state
    reduces to who mints that job — and every door that does refuses unless the
    workspace has `api_publishing_enabled`: `approve` and the review retry by
    `manual_mode`, and the serve step's automatic approval (#1477) by its
    decision's first step.

    In a manual-mode workspace (#1090), no intent can be parked.

    **This is the WEAK gate of the two** — `api_publishing_enabled` is a
    `settings_change` key and that command is built, so an admin can flip it
    without a deploy. It is pinned because it is the first door and its removal
    is worth knowing about; the structural gate is the test above.
    """
    # The FILE, never the line: a line number pins where the producer sits,
    # which every neighbouring edit moves, and says nothing about the
    # property. The property is that the job kind is spelled in one place.
    mints = re.compile(r'kind\s*=\s*["\']publish_pipeline["\']')
    minters = sorted(
        str(p.relative_to(SERVICES))
        for p in _service_sources()
        if mints.search(p.read_text())
    )
    assert minters == ["target/jobs.py"], (
        f"the set of publish_pipeline job producers is now {minters} —"
        " `review_required` may be reachable without api_publishing_enabled."
        " Recheck the review card's resolutions (`resolve_review`)."
    )
    jobs_source = (SERVICES / "target" / "jobs.py").read_text()
    spells_the_kind = sorted(
        node.name
        for node in ast.parse(jobs_source).body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and mints.search(ast.get_source_segment(jobs_source, node) or "")
    )
    assert len(mints.findall(jobs_source)) == 1 and spells_the_kind == [
        "mint_publish_job"
    ], (
        f"the publish_pipeline job kind is spelled by {spells_the_kind} —"
        " it is meant to be minted in exactly one place. See above."
    )

    # WHO CAN REACH THE MINT, not where its text sits: every top-level
    # function in the services that calls it, so a new door cannot slip past
    # by calling the mint instead of spelling the job kind.
    def calls_the_mint(node) -> bool:
        return any(
            isinstance(call, ast.Call)
            and (
                (isinstance(call.func, ast.Name) and call.func.id == "mint_publish_job")
                or (
                    isinstance(call.func, ast.Attribute)
                    and call.func.attr == "mint_publish_job"
                )
            )
            for call in ast.walk(node)
        )

    reaches_the_mint = sorted(
        f"{p.stem}.{node.name}"
        for p in _service_sources()
        for node in ast.parse(p.read_text()).body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name != "mint_publish_job"
        and calls_the_mint(node)
    )
    # `_give_up` mints it too, and parks nothing: it runs only from a row
    # already `review_required`, and its job meets the intent `cancelled` —
    # the pipeline's terminal route destroys the transit copy and finalizes
    # (`publish_pipeline.run_publish_pipeline`), and `park_exhausted` parks
    # no settled story. Reaching `review_required` still needs an approval.
    assert reaches_the_mint == [
        "auto_approval.approve_if_due",
        "command_executors._give_up",
        "command_executors.approve",
        "command_executors.resolve_review",
    ], (
        f"the publish_pipeline job is now minted by {reaches_the_mint} — a"
        " door other than an approval (or the give-up's transit destroy) can"
        " park an intent. See above."
    )

    source = (SERVICES / "target" / "command_executors.py").read_text()
    approve = source.split("async def approve(")[1].split("\nasync def ")[0]
    # The review card's `retry` resolution re-mints the job
    # for an intent that ALREADY passed `approve`'s gate, and re-checks the
    # flag itself — so the manual-mode gate holds at both doors.
    resolve = source.split("async def resolve_review(")[1].split("\nasync def ")[0]
    assert "manual_mode" in resolve, "the retry resolution must keep approve's gate"
    assert '"manual_mode"' in approve, (
        "`approve` no longer refuses in manual mode — the gate that makes the"
        " publish pipeline unreachable for a manual-mode workspace is gone"
        " (#1090, #1124)."
    )
    # The serve step's door, driven: whatever else would approve the story,
    # API posting off approves nothing and mints nothing.
    from src.services.target import auto_approval

    class _FactsSession:
        async def execute(self, statement, params=None):
            row = {
                "provider_account_ref": "ref",
                "cancel_requested": False,
                "api_publishing_enabled": False,
                "dry_run_mode": True,
                "position": "auto",
                "auto_approve": True,
                "has_link": False,
                "has_login": True,
                "posted_via_api": True,
            }

            class _M:
                def first(self_inner):
                    return row

            class _R:
                def mappings(self_inner):
                    return _M()

            return _R()

    # This gate measures manual mode after all media preconditions pass.
    # Canonical SQL locking/readiness is exercised by the actual-role gates.
    from src.services.target import source_policy

    checked = []

    async def suitable(session, *, workspace_id, intent_id):
        checked.append((workspace_id, intent_id))
        return {"suitable": True}

    monkeypatch.setattr(source_policy, "intent_suitability", suitable)

    wrote = []

    async def record(*args, **kwargs):
        wrote.append((args, kwargs))

    monkeypatch.setattr(auto_approval.intent_ledger, "transition", record)
    monkeypatch.setattr(auto_approval.jobs, "mint_publish_job", record)
    rule = asyncio.run(
        auto_approval.approve_if_due(_FactsSession(), workspace_id="ws", intent_id="i")
    )
    assert checked == [("ws", "i")] and rule is None and wrote == [], (
        "the serve step approves a story in a workspace that posts by hand —"
        " the publish pipeline is reachable without api_publishing_enabled"
        " (#1090, #1124)."
    )
