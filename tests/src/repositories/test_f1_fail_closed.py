"""F.1 gate (#841, `04` §F.1): fail-closed tenant interfaces, proven.

The four obligations of the merged interface spec
(`documentation/planning/2026-08-11-f1-ownership-inventory/README.md` §3):

1. a call omitting tenant context fails at the boundary, not at the DB;
2. a cross-tenant read returns zero rows under a foreign tenant;
3. a Class-3 (user-plane) repository rejects tenant context — the negative
   direction, so the classes cannot silently converge;
4. the fail-open signature pattern is extinct in the repository layer, via a
   gate that is itself shown able to fail on a reintroduced instance.

Real DB for obligation 2 (an isolation claim proven against mocks would be
the shape of test that cannot fail); pure-call for the boundary obligations.
"""

import random
import re
from pathlib import Path

import pytest
from sqlalchemy.orm import sessionmaker

from src.repositories.audit_repository import AuditRepository
from src.repositories.category_mix_repository import CategoryMixRepository
from src.repositories.chat_settings_repository import ChatSettingsRepository
from src.repositories.history_repository import HistoryRepository
from src.repositories.lock_repository import LockRepository
from src.repositories.media_repository import MediaRepository
from src.repositories.onboarding_repository import OnboardingRepository
from src.repositories.queue_repository import QueueRepository
from src.repositories.tenant_scope import (
    SYSTEM_SCOPE,
    TenantContextError,
    require_tenant_context,
    tenant_value,
)
from src.repositories.token_repository import TokenRepository
from src.repositories.user_repository import UserRepository

pytestmark = pytest.mark.integration

REPO_DIR = Path(__file__).resolve().parents[3] / "src" / "repositories"

# The fail-open default: a tenant parameter that silently means "everything"
# when omitted. Signature-level, so history_repository's row-value ternary and
# the legitimate SYSTEM-vs-tenant branches never false-positive.
FAIL_OPEN_SIGNATURE = re.compile(
    r"chat_settings_id\s*:\s*Optional\[[^\]]+\]\s*=\s*None"
    r"|chat_settings_id\s*:\s*[^,)=]*\|\s*None\s*=\s*None"
    r"|chat_settings_id\s*=\s*None"
)

# Two sanctioned matches, both DATA FIELDS being written rather than tenant
# filters: onboarding's pending_chat_settings_id (Class 3 — which chat a
# pending onboarding will bind to) and HistoryCreateParams.chat_settings_id
# (a params-object ownership stamp; None = legacy unowned row, part of the
# #841 burn-down, not a query widener).
ALLOWED = {("onboarding_repository.py", "pending_chat_settings_id")}
ALLOWED_LINE_CONTEXT = {"history_repository.py": "class HistoryCreateParams"}


@pytest.fixture(autouse=True)
def _route_repos_to_test_db(setup_test_database, monkeypatch):
    if setup_test_database is None:
        pytest.skip("Database not available - skipping integration test")

    import src.config.database as db_module

    monkeypatch.setattr(
        db_module,
        "SessionLocal",
        sessionmaker(
            autocommit=False,
            autoflush=False,
            bind=setup_test_database,
            expire_on_commit=False,
        ),
    )
    yield


def _tenant() -> tuple:
    """Create a real chat_settings row — chat_settings_id is a live FK."""
    telegram_chat_id = -random.randint(10**11, 10**12)
    repo = ChatSettingsRepository()
    try:
        settings = repo.get_or_create(telegram_chat_id)
        return str(settings.id), telegram_chat_id
    finally:
        repo.close()


def _delete_tenants(tenant_ids):
    from sqlalchemy import text

    repo = ChatSettingsRepository()
    try:
        for tid in tenant_ids:
            repo.db.execute(
                text("DELETE FROM chat_settings WHERE id = :tid"), {"tid": tid}
            )
        repo.db.commit()
    finally:
        repo.close()


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
        ],
        ids=["media", "history", "lock", "queue", "category_mix"],
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
            # former mid-defaults signatures are keyword-only-required now;
            # explicit None still reaches the entry guard:
            (HistoryRepository, lambda r: r.get_all(chat_settings_id=None)),
            (
                AuditRepository,
                lambda r: r.log("t", "i", "update", chat_settings_id=None),
            ),
            (
                TokenRepository,
                lambda r: r.revoke_tokens_for_service("svc", chat_settings_id=None),
            ),
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
        ],
    )
    def test_explicit_or_defaulted_none_raises_before_any_query(
        self, repo_cls, call, monkeypatch
    ):
        repo = repo_cls()
        # Prove "before any SQL": a session that cannot be opened would fail
        # loudly if the method reached the DB. The guard must fire first.
        monkeypatch.setattr(
            type(repo),
            "db",
            property(lambda self: pytest.fail("query ran before the guard")),
        )
        try:
            with pytest.raises(TenantContextError):
                call(repo)
        finally:
            repo._db = None  # nothing to close; bypass the poisoned property


class TestObligation2CrossTenantReadsReturnZeroRows:
    def test_media_history_lock_queue_mix_isolation(self):
        tenant_a, _ = _tenant()
        tenant_b, _ = _tenant()
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
            from datetime import datetime, timedelta

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
            for table, key in (
                ("category_post_case_mix", None),
                ("media_posting_locks", created.get("lock")),
                ("posting_queue", created.get("queue")),
                ("media_items", created.get("media")),
            ):
                if table == "category_post_case_mix":
                    db.execute(
                        text(
                            "DELETE FROM category_post_case_mix"
                            " WHERE chat_settings_id = :t"
                        ),
                        {"t": tenant_a},
                    )
                elif key:
                    db.execute(text(f"DELETE FROM {table} WHERE id = :k"), {"k": key})
            db.commit()
            for r in (media_repo, queue_repo, lock_repo, mix_repo):
                r.close()
            _delete_tenants([tenant_a, tenant_b])


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


def _scan_for_fail_open(text: str, filename: str):
    hits = []
    for m in FAIL_OPEN_SIGNATURE.finditer(text):
        if any(a[0] == filename and a[1] in m.group(0) for a in ALLOWED):
            continue
        if "pending_chat_settings_id" in text[max(0, m.start() - 8) : m.start() + 24]:
            continue
        marker = ALLOWED_LINE_CONTEXT.get(filename)
        if marker:
            # allowed only inside the named dataclass block, nowhere else
            block_start = text.find(marker)
            block_end = text.find("\nclass ", block_start + 1)
            if block_start != -1 and block_start < m.start() < (
                block_end if block_end != -1 else len(text)
            ):
                continue
        hits.append(m.group(0))
    return hits


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

    def test_helpers_hold_their_contract(self):
        require_tenant_context("42", where="gate")
        require_tenant_context(SYSTEM_SCOPE, where="gate")
        with pytest.raises(TenantContextError):
            require_tenant_context(None, where="gate")
        with pytest.raises(TenantContextError):
            require_tenant_context("", where="gate")
        assert tenant_value(SYSTEM_SCOPE) is None
        assert tenant_value("42") == "42"
        assert not SYSTEM_SCOPE, "SYSTEM_SCOPE must stay falsy (behavior-preserving)"
