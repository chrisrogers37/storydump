"""F.1 root-cause gate (#896): evaluate TENANCY, not parameter spelling.

## The defect this replaces, in my own gate

`test_f1_fail_closed.py`'s extinction check keys on the literal string
``chat_settings_id`` in a signature. That answers *"does any method default a
parameter of that NAME to None?"* — which is not the question F.1 exists to
answer. A table whose tenancy lives in a JOIN has **no tenant parameter at all**,
so there is nothing for the regex to match and the gate reports green on a table
it structurally cannot evaluate. `instagram_accounts` was classified
tenant-owned in the F.1 inventory (row 5, risk RF-G2) and sailed straight
through.

**Two instances of the same spelling defect, both mine, both found by building
this:**

1. ``instagram_accounts`` holds **no tenant reference of any kind** — the FK
   runs the other way (`chat_settings.active_instagram_account_id →
   instagram_accounts.id`). Its tenancy is *reverse-derived*, so no spelling
   could have caught it.
2. ``onboarding_sessions`` carries tenancy as ``pending_chat_settings_id`` — and
   the old scanner **explicitly skips anything prefixed `pending_`**. I wrote
   that exemption to dodge a false positive and in doing so exempted the column
   that actually carries the tenancy.

## What this gate does instead

Tenancy is **computed from the schema**, never listed by hand — a hand-list is
the same spelling problem wearing a different hat, and it goes stale the moment
a table is added. From the SQLAlchemy metadata:

- **DIRECT** — the table has a column with a foreign key to the tenant root
  (whatever that column is *named*, which is what fixes defect 2).
- **DERIVED** — no such column, but a tenant-bearing table references it, so its
  rows belong to a tenant only by a join (defect 1's shape).
- **GLOBAL** — neither. User-plane and ops tables live here legitimately.

A repository method that touches a DERIVED table is the hazard: there is no
local column to filter on, so an unscoped query returns every tenant's rows.

## What this does NOT cover — stated because a fix must not imply coverage

`branden` measured, while fixing the five defects that prompted this, that **a
fixed gate would have protected 2 of the 5**. The other three are different
shapes and need their own assertions regardless:

- an **authorization** bypass upstream of the repository layer (#895) — this
  gate never sees the caller;
- a **presentation/aggregation** surface reading across tenants (#898);
- a cross-tenant **WRITE** whose target row was selected elsewhere (#900).

This closes the class where a *repository query* on a derived-tenancy table is
unscoped. It is not a silver bullet and must not be cited as one.
"""

from __future__ import annotations

import ast
import importlib
import pkgutil
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
REPO_DIR = REPO_ROOT / "src" / "repositories"

#: The legacy tenant root. `chat_settings.id` IS the tenant identifier.
TENANT_ROOT = "chat_settings"


def _load_metadata():
    """The SQLAlchemy metadata, with every legacy model imported so the FK
    graph is complete. An incomplete import would silently shrink the graph
    and make the gate under-report — the failure this file exists to end."""
    sys.path.insert(0, str(REPO_ROOT))
    import src.models as M

    for m in pkgutil.iter_modules(M.__path__):
        if m.name != "target":
            importlib.import_module(f"src.models.{m.name}")
    from src.config.database import Base

    return Base.metadata


DIRECT, DERIVED, GLOBAL = "direct", "derived", "global"

#: The adjudication, and the honest part of this design.
#:
#: The schema finds the CANDIDATES — tables with no local tenant column that a
#: tenant-bearing table references — but it cannot decide between them.
#: ``chat_settings.active_instagram_account_id -> instagram_accounts`` and
#: ``chat_settings.user_id -> users`` are structurally identical edges, and an
#: account is OWNED by one tenant while a user is SHARED across many. That is a
#: domain fact, not a schema fact, and pretending to derive it would be a
#: classifier that is confidently wrong.
#:
#: So candidates are resolved here, explicitly, and **an unclassified candidate
#: FAILS the gate** rather than defaulting to safe. Fail-closed is the property
#: F.1 exists for; a classifier that defaulted to GLOBAL would reproduce the
#: exact vacuous pass this issue is about.
CANDIDATE_RULING = {
    # Tenant-OWNED via join: an account belongs to the chat that points at it.
    # This is the #891 case — RF-G2 in the F.1 inventory.
    "instagram_accounts": DERIVED,
    # SHARED user-plane (F.1 Class 3): one user legitimately spans tenants, so
    # a query over users is not a cross-tenant leak.
    "users": GLOBAL,
}


def tenant_candidates(metadata) -> dict[str, list]:
    """Tables with no local tenant column that a tenant-bearing table REFERENCES.

    This is the structural half, and it is the half a spelling check cannot do:
    it finds tables whose tenancy exists only as an inbound edge, which is
    precisely why they have no tenant parameter to inspect.
    """
    tables = metadata.tables
    direct = {
        name
        for name, t in tables.items()
        if any(
            fk.column.table.name == TENANT_ROOT
            for c in t.columns
            for fk in c.foreign_keys
        )
    }
    bearing = direct | {TENANT_ROOT}
    out = {}
    for name, t in tables.items():
        if name in bearing:
            continue
        refs = [
            other
            for other, ot in tables.items()
            if other in bearing
            and any(fk.column.table.name == name for fk in ot.foreign_keys)
        ]
        if refs:
            out[name] = sorted(refs)
    return out


def classify_tenancy(metadata) -> dict[str, str]:
    """Every table's tenancy: computed where the schema decides, adjudicated
    where only the domain can, and never silently defaulted."""
    tables = metadata.tables
    direct = {
        name
        for name, t in tables.items()
        if any(
            fk.column.table.name == TENANT_ROOT
            for c in t.columns
            for fk in c.foreign_keys
        )
    }
    candidates = tenant_candidates(metadata)

    out = {}
    for name in tables:
        if name == TENANT_ROOT or name in direct:
            out[name] = DIRECT
        elif name in candidates:
            # Deliberately raises rather than defaulting — see CANDIDATE_RULING.
            out[name] = CANDIDATE_RULING[name]
        else:
            out[name] = GLOBAL
    return out


@pytest.fixture(scope="module")
def metadata():
    return _load_metadata()


@pytest.fixture(scope="module")
def tenancy(metadata):
    return classify_tenancy(metadata)


class TestTheClassificationIsComputedAndAdjudicated:
    """Equality, not a spot check — the F.6 ratchet's rule. A table changing
    class must arrive as a visible line in a diff."""

    def test_the_CANDIDATE_set_is_found_structurally(self, metadata):
        """The half the schema decides, and the half a spelling check could
        never reach: tables whose tenancy exists ONLY as an inbound edge."""
        cands = tenant_candidates(metadata)
        assert sorted(cands) == ["instagram_accounts", "users"], cands
        assert "chat_settings" in cands["instagram_accounts"], (
            "instagram_accounts is a candidate because chat_settings points AT "
            "it — the reverse edge that leaves it with no tenant parameter"
        )

    def test_an_UNADJUDICATED_candidate_FAILS_rather_than_defaulting(self, metadata):
        """Fail-closed, which is the property F.1 exists for. A classifier that
        defaulted an unknown candidate to GLOBAL would reproduce the exact
        vacuous pass #896 is about, one layer up."""
        saved = CANDIDATE_RULING.pop("instagram_accounts")
        try:
            with pytest.raises(KeyError):
                classify_tenancy(metadata)
        finally:
            CANDIDATE_RULING["instagram_accounts"] = saved

    def test_instagram_accounts_is_DERIVED_and_holds_NO_tenant_reference(
        self, metadata, tenancy
    ):
        """The #891 case, and the reason a parameter check was always going to
        pass vacuously."""
        assert tenancy["instagram_accounts"] == DERIVED
        t = metadata.tables["instagram_accounts"]
        assert not any(c.foreign_keys for c in t.columns), (
            "it references nothing; chat_settings references IT"
        )

    def test_onboarding_sessions_is_DIRECT_despite_its_column_name(self, tenancy):
        """`pending_chat_settings_id` is a real tenant reference, caught because
        the rule reads the FK TARGET rather than the column NAME. The old
        scanner explicitly skipped anything prefixed `pending_`, exempting the
        very column that carries the tenancy."""
        assert tenancy["onboarding_sessions"] == DIRECT

    def test_genuinely_global_tables_are_NOT_flagged(self, tenancy):
        """A gate that cries wolf gets suppressed, and a suppressed gate is
        worse than a loose one. `users` is user-plane by design (F.1 Class 3);
        `service_runs` is ops telemetry that deliberately spans tenants —
        confirmed independently while wiring #882's service-health view."""
        assert tenancy["users"] == GLOBAL
        assert tenancy["service_runs"] == GLOBAL
        assert tenancy["user_interactions"] == GLOBAL

    def test_the_classifier_is_not_vacuous(self, tenancy):
        """Positive control on the classifier itself: a bug classifying
        everything GLOBAL would pass every assertion above except this one."""
        assert set(tenancy.values()) == {DIRECT, DERIVED, GLOBAL}
        assert sum(k == DIRECT for k in tenancy.values()) >= 5


# ---------------------------------------------------------------------------
# Part 2 — repository methods that touch a DERIVED-tenancy table
# ---------------------------------------------------------------------------


#: Model class names for the derived-tenancy tables, which is what repository
#: code actually names. Derived from the metadata rather than hardcoded.
def derived_model_names(metadata, tenancy) -> set:
    out = set()
    for name, kind in tenancy.items():
        if kind != DERIVED:
            continue
        for mapper_name, table in metadata.tables.items():
            if mapper_name == name:
                out.add("".join(p.title() for p in name.rstrip("s").split("_")))
    return out


#: A parameter counts as tenant-shaped if it references the tenant root by ANY
#: name. Spelling-independent on purpose: `pending_chat_settings_id` is a real
#: tenant reference and the old gate skipped it.
TENANT_PARAM = ("chat_settings_id", "chat_settings", "tenant_id", "workspace_id")


def _is_tenant_param(name: str) -> bool:
    return any(t in name for t in TENANT_PARAM)


def scan_unscoped_derived_access(source: str, derived_names: set) -> list:
    """Methods naming a DERIVED-tenancy model with no tenant-shaped parameter.

    The hazard, stated exactly: a derived table has no local tenant column, so a
    query against it returns every tenant's rows unless the method was given a
    tenant to join on. A method that names such a model and accepts no tenant
    is therefore unscoped by construction — which is what
    `instagram_accounts` was.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:  # pragma: no cover
        return []

    findings = []
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if fn.name.startswith("__"):
            continue
        params = [
            a.arg
            for a in fn.args.posonlyargs + fn.args.args + fn.args.kwonlyargs
            if a.arg != "self"
        ]
        if any(_is_tenant_param(p) for p in params):
            continue
        named = {
            n.id
            for n in ast.walk(fn)
            if isinstance(n, ast.Name) and n.id in derived_names
        }
        if named:
            findings.append(f"{fn.name}::{sorted(named)[0]}")
    return sorted(findings)


class TestTheGateCatchesWhatTheOldOneMissed:
    """The proof, planted rather than argued. rajan did exactly this on #914 by
    adding a 16th instance to an untouched file; it is the difference between
    "this is fixed" and "this stays fixed"."""

    def test_a_planted_JOIN_tenancy_hazard_FAILS_BY_NAME(self):
        planted = (
            "class SomeRepository:\n"
            "    def list_all_accounts(self):\n"
            "        return self.db.query(InstagramAccount).all()\n"
        )
        found = scan_unscoped_derived_access(planted, {"InstagramAccount"})
        assert found == ["list_all_accounts::InstagramAccount"], found

    def test_the_OLD_gate_does_NOT_catch_that_same_hazard(self):
        """Side by side, so the improvement is demonstrated rather than
        claimed. The old scanner finds nothing in the exact source above,
        because there is no `chat_settings_id` to spell."""
        from tests.src.repositories.test_f1_fail_closed import _scan_for_fail_open

        planted = (
            "class SomeRepository:\n"
            "    def list_all_accounts(self):\n"
            "        return self.db.query(InstagramAccount).all()\n"
        )
        assert _scan_for_fail_open(planted, "synthetic.py") == [], (
            "if the OLD gate ever catches this, the premise of #896 is wrong"
        )

    def test_a_tenant_scoped_method_is_NOT_flagged(self):
        """Paired negative. Without it the gate could flag everything and still
        pass the test above."""
        ok = (
            "class SomeRepository:\n"
            "    def list_for_chat(self, chat_settings_id):\n"
            "        return self.db.query(InstagramAccount).all()\n"
        )
        assert scan_unscoped_derived_access(ok, {"InstagramAccount"}) == []

    def test_an_ODDLY_NAMED_tenant_param_is_accepted(self):
        """Defect 2's fix: `pending_chat_settings_id` IS a tenant reference, and
        the old gate skipped exactly that prefix."""
        ok = (
            "class R:\n"
            "    def f(self, pending_chat_settings_id):\n"
            "        return self.db.query(InstagramAccount).all()\n"
        )
        assert scan_unscoped_derived_access(ok, {"InstagramAccount"}) == []

    def test_a_method_touching_only_GLOBAL_models_is_NOT_flagged(self):
        """No over-fire on legitimately global tables."""
        ok = (
            "class R:\n"
            "    def all_users(self):\n"
            "        return self.db.query(User).all()\n"
        )
        assert scan_unscoped_derived_access(ok, {"InstagramAccount"}) == []


# ---------------------------------------------------------------------------
# Part 3 — the standing sweep over the real tree
# ---------------------------------------------------------------------------

#: The known burn-down, pinned by EQUALITY rather than tolerated as a ceiling.
#:
#: These 12 methods query `instagram_accounts` — a DERIVED-tenancy table — with
#: no tenant parameter, so each returns every tenant's rows and relies on its
#: CALLER to scope. That is the fail-open pattern F.1 exists to end, and it is
#: the concrete enumeration of a risk F.1 already logged as RF-G2 (inventory
#: row 5) without ever counting it.
#:
#: They are NOT fixed here: #896 is the mechanism, and instance fixes belong in
#: their own change. The five defects that prompted this were fixed in the
#: SERVICE layer (`telegram_accounts.py`, `telegram_service.py`,
#: `telegram_utils.py`) — verified against the fix commits — which is why the
#: repository still reads unscoped. Pinning by equality means a THIRTEENTH
#: arrives as a red test rather than as headroom. Tracked as #923.
KNOWN_UNSCOPED_DERIVED_ACCESS = {
    "instagram_account_repository.py": [
        "activate::InstagramAccount",
        "count_active::InstagramAccount",
        "create::InstagramAccount",
        "deactivate::InstagramAccount",
        "get_all::InstagramAccount",
        "get_all_active::InstagramAccount",
        "get_by_id::InstagramAccount",
        "get_by_id_prefix::InstagramAccount",
        "get_by_instagram_id::InstagramAccount",
        "get_by_meta_account_id::InstagramAccount",
        "get_by_username::InstagramAccount",
        "update::InstagramAccount",
    ]
}


def _derived_model_names(metadata, tenancy) -> set:
    import importlib
    import pkgutil

    import src.models as M

    derived_tables = {n for n, k in tenancy.items() if k == DERIVED}
    names = set()
    for m in pkgutil.iter_modules(M.__path__):
        if m.name == "target":
            continue
        mod = importlib.import_module(f"src.models.{m.name}")
        for attr in dir(mod):
            obj = getattr(mod, attr)
            if getattr(obj, "__tablename__", None) in derived_tables:
                names.add(attr)
    return names


def sweep(metadata, tenancy) -> dict[str, list]:
    names = _derived_model_names(metadata, tenancy)
    out = {}
    for path in sorted(REPO_DIR.glob("*.py")):
        hits = scan_unscoped_derived_access(path.read_text(), names)
        if hits:
            out[path.name] = hits
    return out


class TestTheStandingSweep:
    def test_the_sweep_matches_the_pinned_burn_down_exactly(self, metadata, tenancy):
        """Equality, not a ceiling. A new unscoped method on a derived-tenancy
        table fails here; retiring one is a visible line in the same diff."""
        assert sweep(metadata, tenancy) == KNOWN_UNSCOPED_DERIVED_ACCESS

    def test_the_sweep_actually_reached_the_tree(self, metadata, tenancy):
        """A sweep asserting an empty-or-known result passes vacuously against
        a tree it could not read. Three positive controls, because each covers
        what the others cannot."""
        assert len(list(REPO_DIR.glob("*.py"))) > 5, "no repositories scanned"
        assert _derived_model_names(metadata, tenancy) == {"InstagramAccount"}
        assert sum(len(v) for v in sweep(metadata, tenancy).values()) == 12

    def test_the_three_named_repositories_are_OUT_OF_THIS_GATES_SCOPE(
        self, metadata, tenancy
    ):
        """#896 asked what this finds in ChatSettings/Interaction/Membership.

        The answer is ZERO, and the honest reading is NOT "those three are
        safe" — it is that this gate's shape does not apply to them. None
        touches a derived-tenancy table: `chat_settings` IS the tenant root,
        `user_chat_memberships` carries its own tenant column (DIRECT), and
        `user_interactions` is user-plane with no tenant path at all (GLOBAL).
        So a zero here is a statement about coverage, not about safety —
        reporting it as a clean bill of health would be the exact vacuous-green
        this whole issue is about, one layer up.
        """
        found = sweep(metadata, tenancy)
        for name in (
            "chat_settings_repository.py",
            "interaction_repository.py",
            "membership_repository.py",
        ):
            assert (REPO_DIR / name).exists(), f"{name} must exist to be scanned"
            assert name not in found
        assert tenancy["chat_settings"] == DIRECT
        assert tenancy["user_chat_memberships"] == DIRECT
        assert tenancy["user_interactions"] == GLOBAL
