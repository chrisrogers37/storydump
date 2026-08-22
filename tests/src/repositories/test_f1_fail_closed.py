"""F.1 gate (#841, `04` §F.1): fail-closed tenant interfaces, proven.

The four obligations of the merged interface spec
(`documentation/planning/2026-08-11-f1-ownership-inventory/README.md` §3):

1. a call omitting tenant context fails at the boundary, not at the DB;
2. a cross-tenant read returns zero rows under a foreign tenant;
3. a Class-3 (user-plane) repository rejects tenant context — the negative
   direction, so the classes cannot silently converge;
4. the fail-open signature pattern is extinct in the repository layer, via a
   gate that is itself shown able to fail on a reintroduced instance.

Only obligation 2 needs a database (an isolation claim proven against mocks
would be the shape of test that cannot fail); everything else here runs in
any environment, deliberately — the durable gates must not silently skip
where Postgres is absent.
"""

import re
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import pytest

from src.repositories.audit_repository import AuditRepository
from src.repositories.base_repository import BaseRepository
from src.repositories.category_mix_repository import CategoryMixRepository
from src.repositories.history_repository import HistoryRepository
from src.repositories.lock_repository import LockRepository
from src.repositories.media_repository import MediaRepository
from src.repositories.onboarding_repository import OnboardingRepository
from src.repositories.queue_repository import QueueRepository
from src.models.posting_queue import PostingQueue
from src.repositories.tenant_scope import (
    SYSTEM_SCOPE,
    TenantContextError,
    require_tenant_context,
    require_tenant_id,
    scope_of_row,
    tenant_value,
    write_allowed,
)
from src.repositories.token_repository import TokenRepository
from src.repositories.user_repository import UserRepository
from tests.conftest import delete_tenants, make_tenant

REPO_ROOT = Path(__file__).resolve().parents[3]
REPO_DIR = REPO_ROOT / "src" / "repositories"

# The fail-open default: a tenant parameter that silently means "everything"
# when omitted. Signature-level on purpose — history_repository's row-value
# ternary and the legitimate SYSTEM-vs-tenant branches can never match.
FAIL_OPEN_SIGNATURE = re.compile(
    r"chat_settings_id\s*:\s*Optional\[[^\]]+\]\s*=\s*None"
    r"|chat_settings_id\s*:\s*[^,)=]*\|\s*None\s*=\s*None"
    r"|chat_settings_id\s*=\s*None"
)

# Sanctioned matches, both DATA FIELDS being written rather than tenant
# filters. Exemption mechanics: onboarding's `pending_chat_settings_id` is
# excluded by the prefix window below; HistoryCreateParams.chat_settings_id
# (a params-object ownership stamp; None = legacy unowned row, tracked on the
# #841 burn-down) is excluded by its dataclass block span.
_EXEMPT_BLOCK = {"history_repository.py": "class HistoryCreateParams"}


def _scan_for_fail_open(text: str, filename: str):
    exempt_start = exempt_end = -1
    marker = _EXEMPT_BLOCK.get(filename)
    if marker and marker in text:
        exempt_start = text.find(marker)
        nxt = text.find("\nclass ", exempt_start + 1)
        exempt_end = nxt if nxt != -1 else len(text)

    hits = []
    for m in FAIL_OPEN_SIGNATURE.finditer(text):
        if text[max(0, m.start() - 8) : m.start()].endswith("pending_"):
            continue
        if exempt_start != -1 and exempt_start < m.start() < exempt_end:
            continue
        hits.append(m.group(0))
    return hits


class TestObligation1AbsentContextFailsAtTheBoundary:
    """Omission is a TypeError; explicit None raises before any SQL runs."""

    @pytest.mark.parametrize(
        "repo_cls,call",
        [
            (MediaRepository, lambda r: r.get_by_id("x")),
            (HistoryRepository, lambda r: r.get_by_id("x")),
            (LockRepository, lambda r: r.get_by_id("x")),
            (QueueRepository, lambda r: r.get_by_id("x")),
            (CategoryMixRepository, lambda r: r.get_current_mix()),
            (QueueRepository, lambda r: r.delete("x")),
            (QueueRepository, lambda r: r.update_status("x", "failed")),
            (QueueRepository, lambda r: r.transition("x", "failed")),
            (QueueRepository, lambda r: r.mark_publishing("x", "c")),
            (QueueRepository, lambda r: r.update_scheduled_time("x", None)),
            (QueueRepository, lambda r: r.set_telegram_message("x", 1, 2)),
        ],
        ids=[
            "media",
            "history",
            "lock",
            "queue",
            "category_mix",
            "queue_delete",
            "queue_update_status",
            "queue_transition",
            "queue_mark_publishing",
            "queue_update_scheduled_time",
            "queue_set_telegram_message",
        ],
    )
    def test_omission_is_a_typeerror(self, repo_cls, call):
        repo = repo_cls()
        try:
            with pytest.raises(TypeError):
                call(repo)
        finally:
            repo.close()

    @pytest.mark.parametrize(
        "repo_cls,call",
        [
            (MediaRepository, lambda r: r.get_by_id("x", None)),
            (HistoryRepository, lambda r: r.get_by_id("x", None)),
            (LockRepository, lambda r: r.get_by_id("x", None)),
            (QueueRepository, lambda r: r.get_by_id("x", None)),
            (CategoryMixRepository, lambda r: r.get_current_mix(None)),
            (HistoryRepository, lambda r: r.get_all(chat_settings_id=None)),
            (
                AuditRepository,
                lambda r: r.log("t", "i", "update", chat_settings_id=None),
            ),
            (
                TokenRepository,
                lambda r: r.revoke_tokens_for_service("svc", chat_settings_id=None),
            ),
            (QueueRepository, lambda r: r.delete("x", None)),
            (QueueRepository, lambda r: r.transition("x", "failed", None)),
            (QueueRepository, lambda r: r.mark_publishing("x", "c", None)),
            (QueueRepository, lambda r: r.set_telegram_message("x", 1, 2, None)),
        ],
        ids=[
            "media-none",
            "history-none",
            "lock-none",
            "queue-none",
            "category_mix-none",
            "history-kwonly-none",
            "audit-kwonly-none",
            "token-kwonly-none",
            "queue_delete-none",
            "queue_transition-none",
            "queue_mark_publishing-none",
            "queue_set_telegram_message-none",
        ],
    )
    def test_explicit_none_raises_before_any_query(self, repo_cls, call, monkeypatch):
        repo = repo_cls()
        # Prove "before any SQL" AND before any session checkout: a .db
        # access would fail the test, so the refusal must come first.
        monkeypatch.setattr(
            type(repo),
            "db",
            property(lambda self: pytest.fail("session touched before the guard")),
        )
        try:
            with pytest.raises(TenantContextError):
                call(repo)
        finally:
            repo._db = None  # nothing to close; bypass the poisoned property

    def test_mandatory_tenant_methods_refuse_system_scope(self, monkeypatch):
        """The *_for_chat family has no cross-tenant door: SYSTEM_SCOPE would
        bind the marker object into SQL, so it is refused alongside None."""
        repo = TokenRepository()
        monkeypatch.setattr(
            type(repo),
            "db",
            property(lambda self: pytest.fail("session touched before the guard")),
        )
        try:
            with pytest.raises(TenantContextError):
                repo.get_token_for_chat("svc", "access", chat_settings_id=SYSTEM_SCOPE)
        finally:
            repo._db = None


class TestObligation2CrossTenantReadsReturnZeroRows:
    pytestmark = [pytest.mark.integration]

    @pytest.fixture(autouse=True)
    def _db(self, route_repos_to_test_db):
        yield

    def test_media_history_lock_queue_mix_isolation(self):
        from datetime import datetime, timedelta

        tenant_a, _ = make_tenant()
        tenant_b, _ = make_tenant()
        media_repo = MediaRepository()
        queue_repo = QueueRepository()
        lock_repo = LockRepository()
        mix_repo = CategoryMixRepository()
        created = {}
        try:
            item = media_repo.create(
                file_path=f"/f1/{tenant_a}.jpg",
                file_name="f1.jpg",
                file_hash=f"f1-{tenant_a}",
                file_size_bytes=1,
                chat_settings_id=tenant_a,
            )
            created["media"] = str(item.id)
            q = queue_repo.create(
                media_item_id=str(item.id),
                scheduled_for=datetime.utcnow() + timedelta(days=1),
                chat_settings_id=tenant_a,
            )
            created["queue"] = str(q.id)
            lock = lock_repo.create(
                media_item_id=str(item.id),
                ttl_days=1,
                chat_settings_id=tenant_a,
            )
            created["lock"] = str(lock.id)
            mix_repo.set_mix({"cat": 1.0}, chat_settings_id=tenant_a)

            # Owner sees its rows; a foreign tenant sees zero of them.
            assert media_repo.get_by_id(created["media"], tenant_a) is not None
            assert media_repo.get_by_id(created["media"], tenant_b) is None
            assert queue_repo.get_by_id(created["queue"], tenant_b) is None
            assert lock_repo.get_by_id(created["lock"], tenant_b) is None
            assert mix_repo.get_current_mix_as_dict(chat_settings_id=tenant_a)
            assert not mix_repo.get_current_mix_as_dict(chat_settings_id=tenant_b)
        finally:
            from sqlalchemy import text

            db = media_repo.db
            db.execute(
                text("DELETE FROM category_post_case_mix WHERE chat_settings_id = :t"),
                {"t": tenant_a},
            )
            for table, key in (
                ("media_posting_locks", created.get("lock")),
                ("posting_queue", created.get("queue")),
                ("media_items", created.get("media")),
            ):
                if key:
                    db.execute(text(f"DELETE FROM {table} WHERE id = :k"), {"k": key})
            db.commit()
            for r in (media_repo, queue_repo, lock_repo, mix_repo):
                r.close()
            delete_tenants([tenant_a, tenant_b])


class TestObligation3ClassThreeRejectsTenantContext:
    """User-plane repositories must not accept tenant context at all —
    the negative direction, so Class 1 and Class 3 cannot silently
    converge (spec §3 Rule 2)."""

    def test_user_repository_rejects_tenant_kwarg(self):
        repo = UserRepository()
        try:
            with pytest.raises(TypeError):
                repo.get_by_id("x", chat_settings_id="1")
        finally:
            repo.close()

    def test_onboarding_repository_rejects_tenant_kwarg(self):
        repo = OnboardingRepository()
        try:
            with pytest.raises(TypeError):
                repo.get_by_id("x", chat_settings_id="1")
        finally:
            repo.close()


class TestObligation4TheFailOpenSignatureIsExtinct:
    def test_no_repository_signature_defaults_tenant_to_none(self):
        offenders = {}
        for path in sorted(REPO_DIR.glob("*.py")):
            hits = _scan_for_fail_open(path.read_text(), path.name)
            if hits:
                offenders[path.name] = hits
        assert not offenders, f"fail-open tenant defaults reintroduced: {offenders}"

    def test_the_gate_can_fail_on_a_reintroduced_instance(self):
        """A gate that cannot fail proves nothing (spec §3's own rule)."""
        reintroduced = "def get_all(self, chat_settings_id: Optional[str] = None):"
        assert _scan_for_fail_open(reintroduced, "synthetic.py")

    def test_system_scope_inventory_is_pinned_exactly(self):
        """The #841 burn-down census: SYSTEM_SCOPE passed as a call argument
        in src/ + cli/ — the one shape that grants cross-tenant access.

        Counted by AST (call arguments only), never by substring: imports,
        docstrings and comments are not access sites, and a raw text count
        gave a security number three unrelated things could move (#846
        review — 24 of its 88 were imports and prose, and retiring a file's
        last site dropped the count by 2).

        Pinned by EQUALITY, not a ceiling: a ceiling accrues headroom as the
        burn-down retires sites, and a new cross-tenant call could ride in
        on headroom it did not create. With equality, any delta in either
        direction is red until this constant moves in the same PR — so a new
        site always appears in review next to the +1 that admits it, and
        burn-down progress always lowers the pin in the diff that earns it.

        Stated blind spot: an alias (`S = SYSTEM_SCOPE`) or an attribute
        re-export would evade the Name match. `scope_of_row` is the one
        sanctioned derivation — it RESOLVES to a real tenant id for a stamped
        row and only falls back to SYSTEM_SCOPE for a legacy unstamped one,
        which it logs at WARNING. That residual is deliberately observable in
        production rather than counted here, because its size is a property
        of the DATA (how much predates the #412 backfill), not of the source,
        and a source count would report it as zero while it is not.

        Burn-down history: 64 → 48 (#841 items 1+3). 12 telegram second-hop
        reads now scope to the row the caller already holds; four
        QueueRepository by-identity mutators collapse into the shared
        ownership-checked `BaseRepository._get_for_write`, whose own
        identity-first read keeps ONE marked site where five were unmarked.

        48 → 49 (#512). `ChatSettingsRepository.get_by_id` becomes tenant-scoped
        like every other by-id read, so its callers must now name a scope. Three
        of the four are self-scoped — they ask for the tenant they are already
        acting as. The fourth, `TelegramNotificationHandlers`' resolution of the
        tenant that owns a queue item, is a genuine foreign-key dereference off
        a row the caller already holds: the same shape as
        `QueueRepository.get_by_id_any_tenant` and `LockRepository`'s by-id
        read, and it is marked rather than hidden. This is the pin moving UP by
        exactly the one site that admits it, which is what the paragraph above
        asks for.
        """
        import ast as _ast

        pinned = 49
        count = 0
        offenders = {}
        for d in ("src", "cli"):
            for path in (REPO_ROOT / d).rglob("*.py"):
                if path.name == "tenant_scope.py":
                    continue
                tree = _ast.parse(path.read_text())
                n = 0
                for node in _ast.walk(tree):
                    if isinstance(node, _ast.Call):
                        n += sum(
                            1
                            for kw in node.keywords
                            if isinstance(kw.value, _ast.Name)
                            and kw.value.id == "SYSTEM_SCOPE"
                        )
                        n += sum(
                            1
                            for a in node.args
                            if isinstance(a, _ast.Name) and a.id == "SYSTEM_SCOPE"
                        )
                if n:
                    offenders[str(path.relative_to(REPO_ROOT))] = n
                    count += n
        assert count == pinned, (
            f"SYSTEM_SCOPE call-argument census is {count}, pin is {pinned}. "
            f"A new deliberate cross-tenant site must move the pin UP in its "
            f"own PR (reviewed, next to the site that admits it); burn-down "
            f"progress moves it DOWN in the diff that earns it. Census: "
            f"{offenders}"
        )

    def test_helpers_hold_their_contract(self):
        require_tenant_context("42", where="gate")
        require_tenant_context(SYSTEM_SCOPE, where="gate")
        with pytest.raises(TenantContextError):
            require_tenant_context(None, where="gate")
        with pytest.raises(TenantContextError):
            require_tenant_context("", where="gate")
        with pytest.raises(TenantContextError):
            require_tenant_id(SYSTEM_SCOPE, where="gate")
        with pytest.raises(TenantContextError):
            require_tenant_id(None, where="gate")
        require_tenant_id("42", where="gate")
        assert tenant_value(SYSTEM_SCOPE) is None
        assert tenant_value("42") == "42"
        assert not SYSTEM_SCOPE, "SYSTEM_SCOPE must stay falsy (behavior-preserving)"


class TestScopeOfRowCarriesTheRowsOwnScope:
    """A second-hop read is scoped by the row the caller ALREADY HOLDS.

    Before #841 items 1+3 these sites passed SYSTEM_SCOPE — the whole estate
    granted in order to fetch one row's own media. The helper is the narrowing,
    so its three outcomes are pinned here rather than left to the call sites.
    """

    class _Row:
        def __init__(self, stamp):
            self.id = "row-1"
            self.chat_settings_id = stamp

    def test_a_stamped_row_scopes_to_its_stamp(self):
        assert scope_of_row(self._Row("tenant-A"), where="t") == "tenant-A"

    def test_a_stamped_row_scope_is_a_string_not_the_column_object(self):
        """UUID columns come back as UUID objects; the tenant filter compares
        against strings everywhere else in the layer."""
        from uuid import uuid4

        u = uuid4()
        assert scope_of_row(self._Row(u), where="t") == str(u)

    def test_an_unstamped_legacy_row_falls_back_to_system_scope(self):
        assert scope_of_row(self._Row(None), where="t") is SYSTEM_SCOPE

    def test_the_legacy_fallback_is_logged_not_silent(self):
        """The residual is a DATA property the source census cannot see, so
        production observability is the only place it can be counted."""
        with patch("src.repositories.tenant_scope.logger") as mock_logger:
            scope_of_row(self._Row(None), where="t")
            mock_logger.warning.assert_called_once()

    def test_a_stamped_row_logs_nothing(self):
        """The positive control: a warning on every call would be no signal."""
        with patch("src.repositories.tenant_scope.logger") as mock_logger:
            scope_of_row(self._Row("tenant-A"), where="t")
            mock_logger.warning.assert_not_called()

    def test_none_is_refused_rather_than_widened(self):
        """Returning SYSTEM_SCOPE for an absent row would rebuild the
        fail-open default one layer up — "no context means everything"."""
        with pytest.raises(TenantContextError):
            scope_of_row(None, where="t")


@contextmanager
def owned_queue_bed(tag):
    """Two real tenants, one media item owned by the first, and a purge.

    The two integration classes below both need exactly this, and a teardown
    fixed in one copy but not the other is a leaked row that fails the NEXT
    run — so there is one copy. Deletes are explicit because
    ``route_repos_to_test_db`` rolls nothing back and ``repo.create`` commits.
    """
    from sqlalchemy import text

    owner, _ = make_tenant()
    stranger, _ = make_tenant()
    media_repo = MediaRepository()
    repo = QueueRepository()
    rows = []
    media_id = None
    try:
        item = media_repo.create(
            file_path=f"/{tag}/{owner}.jpg",
            file_name=f"{tag}.jpg",
            file_hash=f"{tag}-{owner}",
            file_size_bytes=1,
            chat_settings_id=owner,
        )
        media_id = str(item.id)
        yield owner, stranger, repo, media_id, rows
    finally:
        for row_id in rows:
            repo.db.execute(
                text("DELETE FROM posting_queue WHERE id = :i"), {"i": row_id}
            )
        if media_id:
            repo.db.execute(
                text("DELETE FROM media_items WHERE id = :m"), {"m": media_id}
            )
        repo.db.commit()
        repo.close()
        media_repo.close()
        delete_tenants([owner, stranger])


class TestTheOwnershipRuleHasOneMeaningInTwoLanguages:
    """`write_allowed` (Python) and `_owned_or_null` (SQL) are twins: the
    fetch-then-mutate path uses the first, and the single-statement
    conditional UPDATE — whose atomicity is the point — needs the second.

    Two expressions of one rule is a fork risk. They are pinned against each
    other here, and the SQL side is evaluated BY THE DATABASE against real
    rows: a clause compared to a hand-written Python re-statement of itself
    would agree by construction and prove nothing.
    """

    pytestmark = [pytest.mark.integration]

    @pytest.fixture(autouse=True)
    def _db(self, route_repos_to_test_db):
        yield

    # (owner stamp, caller scope, may the caller write it?)
    CASES = [
        ("own", "own", True),
        ("other", "own", False),  # the #597 hole
        (None, "own", True),  # legacy NULL-owned, pre-#412 backfill
        ("other", SYSTEM_SCOPE, True),
        (None, SYSTEM_SCOPE, True),
    ]

    def test_the_sql_clause_and_the_python_predicate_agree(self):
        from datetime import datetime, timedelta

        with owned_queue_bed("twin") as (owner, other, repo, media_id, rows):
            resolve = {
                "own": owner,
                "other": other,
                None: None,
                SYSTEM_SCOPE: SYSTEM_SCOPE,
            }
            for stamp, caller, expected in self.CASES:
                row = repo.create(
                    media_item_id=media_id,
                    scheduled_for=datetime.utcnow() + timedelta(days=1),
                    chat_settings_id=resolve[stamp] or SYSTEM_SCOPE,
                )
                rows.append(str(row.id))
                caller_scope = resolve[caller]

                # Python side.
                assert write_allowed(resolve[stamp], caller_scope) is expected, (
                    stamp,
                    caller,
                    "python",
                )

                # SQL side — the clause, run by Postgres against the real row.
                q = (
                    repo.db.query(PostingQueue)
                    .filter(PostingQueue.id == str(row.id))
                    .filter(*BaseRepository._owned_or_null(PostingQueue, caller_scope))
                )
                assert (q.first() is not None) is expected, (stamp, caller, "sql")

    def test_system_scope_gets_no_clause_at_all(self):
        assert BaseRepository._owned_or_null(PostingQueue, SYSTEM_SCOPE) == ()

    def test_a_tenant_caller_gets_a_restricting_clause(self):
        """The negative direction — if this returned None for a real tenant,
        every conditional UPDATE would silently run unscoped."""
        assert BaseRepository._owned_or_null(PostingQueue, "tenant-A") != ()


class TestQueueMutatorsRefuseAForeignTenant:
    """#841 item 3: the by-identity mutators took no tenant context at all —
    knowing a UUID was sufficient to delete, reschedule or re-status another
    tenant's queue row. The read side was scoped; the write side was not, which
    is the fail-open shape F.1 exists to extinguish.

    A refusal returns the same answer as "not found" on purpose: a caller must
    not be able to probe another tenant's queue by UUID.
    """

    pytestmark = [pytest.mark.integration]

    @pytest.fixture(autouse=True)
    def _db(self, route_repos_to_test_db):
        yield

    def test_a_foreign_tenant_cannot_mutate_or_delete(self):
        from datetime import datetime, timedelta

        with owned_queue_bed("xt") as (owner, stranger, repo, media_id, rows):
            row = repo.create(
                media_item_id=media_id,
                scheduled_for=datetime.utcnow() + timedelta(days=1),
                chat_settings_id=owner,
            )
            queue_id = str(row.id)
            rows.append(queue_id)

            # Fetch-then-mutate family: refused, and the row is untouched.
            assert repo.mark_publishing(queue_id, "c-1", stranger) is None
            assert repo.set_telegram_message(queue_id, 1, 2, stranger) is None
            assert (
                repo.update_scheduled_time(queue_id, datetime.utcnow(), stranger)
                is None
            )

            # Conditional-UPDATE family: the ownership rule rides in the WHERE
            # clause, so a foreign caller matches zero rows.
            assert repo.update_status(queue_id, "failed", stranger) is None
            assert (
                repo.transition(queue_id, "failed", stranger, allowed_from={"pending"})
                is None
            )

            still = repo.get_by_id(queue_id, chat_settings_id=owner)
            assert still is not None, "a refused write must not delete the row"
            assert still.status == "pending", "a refused write must not change status"
            assert still.instagram_container_id is None
            assert still.telegram_message_id is None

            # Delete is the same rule, and its False is indistinguishable from
            # not-found — no probing another tenant's queue by UUID.
            assert repo.delete(queue_id, stranger) is False
            assert repo.get_by_id(queue_id, chat_settings_id=owner) is not None

            # The owner is unaffected by any of it — the positive control,
            # without which a method that refused EVERYONE would pass.
            assert repo.update_status(queue_id, "processing", owner) is not None
            assert repo.delete(queue_id, owner) is True
            rows.remove(queue_id)
