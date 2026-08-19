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
from pathlib import Path

import pytest

from src.repositories.audit_repository import AuditRepository
from src.repositories.category_mix_repository import CategoryMixRepository
from src.repositories.history_repository import HistoryRepository
from src.repositories.lock_repository import LockRepository
from src.repositories.media_repository import MediaRepository
from src.repositories.onboarding_repository import OnboardingRepository
from src.repositories.queue_repository import QueueRepository
from src.repositories.tenant_scope import (
    SYSTEM_SCOPE,
    TenantContextError,
    require_tenant_context,
    require_tenant_id,
    tenant_value,
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
        re-export would evade the Name match. No such form exists today;
        introducing one moves this count DOWN, which the equality pin also
        refuses — the evasion is loud, not silent.
        """
        import ast as _ast

        pinned = 64
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
