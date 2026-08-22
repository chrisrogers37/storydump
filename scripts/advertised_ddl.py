"""Advertised-DDL extractor (plan §0.2 `advertised_ddl_replay`, #753 PR-B).

`02` §0 is the one normative home of the advertised stream's definition:

    the literal ```sql blocks (illustrative transaction shapes —
    bind-parameter / BEGIN; examples — excluded) PLUS the mechanical
    expansion of the two policy lists §7-DDL declares by pattern.

This module extracts that stream from the plan documents so a replay fixture
can prove the plan text and the F.2 migration files stay in lockstep. It is
STANDALONE by the same rule as the migration runner: stdlib only, zero `src`
imports, so the gate has no runtime coupling to the app it describes.

The classification of each block — normative (replayed) vs illustrative
(excluded) — is NOT inferred by a regex over the SQL. It is a reviewed fact
recorded in a committed manifest keyed by each block's content hash, and a
CI ratchet asserts the manifest and the docs are an exact bijection: any
block added, removed, or edited in the plan falls out of the manifest and
fails the gate until a human re-classifies it. "Illustrative" is a decision
on the record, never a guess (the reason literal-only replay was wrong —
pass 6/R5 — and the reason a pure heuristic would be wrong too).
"""

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_PLAN_DIR = (
    _HERE.parent / "documentation" / "planning" / "2026-08-02-consolidated-design-plan"
)

#: Source docs, in the normative replay order (`02` §0): this file
#: top-to-bottom, then `07` in file order.
DEFAULT_DOCS = (_PLAN_DIR / "02-domain-model.md", _PLAN_DIR / "07-security-model.md")
DEFAULT_MANIFEST = _HERE / "advertised_ddl_manifest.json"

_FENCE_RE = re.compile(r"^```sql[ \t]*\n(.*?)\n```[ \t]*$", re.DOTALL | re.MULTILINE)


class AdvertisedDDLError(Exception):
    """A manifest/doc inconsistency the ratchet must not paper over."""


@dataclass(frozen=True)
class Block:
    """One ```sql fenced block, content-addressed."""

    doc: Path
    ordinal: int  # 1-based position within its own document
    sql: str
    sha256: str


@dataclass(frozen=True)
class ManifestEntry:
    sha256: str
    class_: str  # normative | illustrative
    label: str
    reason: str | None


@dataclass(frozen=True)
class Manifest:
    blocks: tuple
    tenant_policy_tables: tuple
    user_policy_tables: tuple

    def by_hash(self) -> dict:
        return {e.sha256: e for e in self.blocks}


@dataclass(frozen=True)
class RatchetReport:
    unclassified: tuple  # Block instances in docs but not the manifest
    orphaned: tuple  # manifest hashes with no matching block

    @property
    def ok(self) -> bool:
        return not self.unclassified and not self.orphaned


def _normalize(sql: str) -> str:
    """Content-hash input: strip trailing whitespace per line, drop a trailing
    blank line. Stable against cosmetic reflow, sensitive to real change."""
    lines = [line.rstrip() for line in sql.replace("\r\n", "\n").split("\n")]
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines)


def extract_blocks(doc) -> list:
    """Every ```sql fenced block in ``doc``, in file order, content-hashed."""
    doc = Path(doc)
    text = doc.read_text()
    blocks = []
    for ordinal, match in enumerate(_FENCE_RE.finditer(text), start=1):
        sql = _normalize(match.group(1))
        blocks.append(
            Block(
                doc=doc,
                ordinal=ordinal,
                sql=sql,
                sha256=hashlib.sha256(sql.encode()).hexdigest(),
            )
        )
    return blocks


def load_manifest(path) -> Manifest:
    data = json.loads(Path(path).read_text())
    entries = []
    seen = set()
    for raw in data["blocks"]:
        cls = raw["class"]
        if cls not in ("normative", "illustrative"):
            raise AdvertisedDDLError(
                f"block {raw.get('label', raw['sha256'][:8])}: class must be"
                f" normative|illustrative, got {cls!r}"
            )
        reason = raw.get("reason")
        if cls == "illustrative" and not (reason and reason.strip()):
            raise AdvertisedDDLError(
                f"illustrative block {raw.get('label', raw['sha256'][:8])} needs"
                " a reason — 'excluded' is a decision on the record, not a guess"
            )
        if raw["sha256"] in seen:
            raise AdvertisedDDLError(f"manifest lists {raw['sha256'][:12]} twice")
        seen.add(raw["sha256"])
        entries.append(ManifestEntry(raw["sha256"], cls, raw["label"], reason))
    expansion = data.get("policy_expansion", {})
    return Manifest(
        blocks=tuple(entries),
        tenant_policy_tables=tuple(expansion.get("tenant_plane", [])),
        user_policy_tables=tuple(expansion.get("user_plane", [])),
    )


def ratchet_report(docs, manifest: Manifest) -> RatchetReport:
    """The F.6-ratchet gate: the manifest and the docs must be an exact
    bijection. A block the manifest does not classify is a defect (a new or
    edited plan block); a manifest hash no block matches is a defect (a
    deleted or edited block whose classification is now stale)."""
    return _ratchet([b for doc in docs for b in extract_blocks(doc)], manifest)


def _ratchet(doc_blocks, manifest: Manifest) -> RatchetReport:
    """The bijection check over already-extracted blocks — shared by
    ``ratchet_report`` and ``build_stream`` so neither re-parses the docs."""
    classified = manifest.by_hash()
    unclassified = tuple(b for b in doc_blocks if b.sha256 not in classified)
    present = {b.sha256 for b in doc_blocks}
    orphaned = tuple(sha for sha in classified if sha not in present)
    return RatchetReport(unclassified=unclassified, orphaned=orphaned)


def expand_policies(policy_name, tables, key_column, roles) -> str:
    """Generate one RLS policy per table from the §7-DDL exemplar.

    Two shapes, exactly as the two exemplars print them: a tenant-plane pair
    keyed on ``key_column`` against the ``app.tenant_id`` GUC, and an open
    user-plane policy (``USING (true) WITH CHECK (true)``). The role list is
    passed in so a caller can never accidentally widen it beyond the
    exemplar's ``svc_ingress, svc_worker``.
    """
    role_clause = ", ".join(roles)
    out = []
    for table in tables:
        if key_column is None:
            out.append(
                f"CREATE POLICY {policy_name} ON {table} FOR ALL TO {role_clause}\n"
                f"  USING (true) WITH CHECK (true);"
            )
        else:
            pred = f"{key_column} = NULLIF(current_setting('app.tenant_id', true), '')::uuid"
            out.append(
                f"CREATE POLICY {policy_name} ON {table} FOR ALL TO {role_clause}\n"
                f"  USING      ({pred})\n"
                f"  WITH CHECK ({pred});"
            )
    return "\n".join(out)


def build_stream(docs, manifest: Manifest) -> str:
    """The advertised stream: normative blocks in file order, plus the two
    §7-DDL policy-list expansions appended after the block that declares them.

    Fails closed if a block is unclassified — the stream can only be built
    over a satisfied ratchet, so it never silently drops or admits a block.
    """
    doc_blocks = [b for doc in docs for b in extract_blocks(doc)]
    report = _ratchet(doc_blocks, manifest)
    if not report.ok:
        raise AdvertisedDDLError(
            "cannot build the stream over an unsatisfied ratchet:"
            f" {len(report.unclassified)} unclassified,"
            f" {len(report.orphaned)} orphaned — reconcile the manifest first"
        )
    by_hash = manifest.by_hash()
    _ROLES = ["svc_ingress", "svc_worker"]
    generated = "\n".join(
        [
            expand_policies(
                "p_tenant", list(manifest.tenant_policy_tables), "workspace_id", _ROLES
            ),
            expand_policies(
                "p_user_plane", list(manifest.user_policy_tables), None, _ROLES
            ),
        ]
    ).strip()

    parts = []
    for block in doc_blocks:
        entry = by_hash[block.sha256]
        if entry.class_ == "illustrative":
            continue
        parts.append(f"-- [{entry.label}]\n{block.sql}")
        # The policy expansion belongs with the §7-DDL grants/policies
        # block that declares the two lists by pattern.
        if _declares_policy_lists(block.sql):
            parts.append(
                "-- [generated: §7-DDL policy-list expansion, 13 tenant"
                " + 2 user]\n" + generated
            )
    return "\n\n".join(parts) + "\n"


def _declares_policy_lists(sql: str) -> bool:
    """The §7-DDL block that prints the two exemplars and names the lists —
    identified by the exemplar policies it carries, not by position."""
    return (
        "CREATE POLICY p_tenant_workspaces ON workspaces" in sql
        and "CREATE POLICY p_user_plane ON users" in sql
    )


# --- arm (b): the F.2 prefix-diff ------------------------------------------
#
# The plan (§0.2 arm b) holds the concatenated F.2 migration files equal to
# the advertised stream "so the gate tests exactly what F.2 installs". F.2 is
# an active lane (alex's), so the comparison is a PREFIX: whatever F.2 has
# built so far must equal the stream's opening statements, in order. When F.2
# completes, the prefix becomes the whole stream and equality is total. The
# comparison is at the normalized-statement level, robust to each migration
# file's own comment/whitespace scaffolding but sensitive to a real DDL
# divergence between the plan and its implementation.


def _strip_comment_lines(sql: str) -> str:
    kept = []
    for line in sql.split("\n"):
        stripped = line.strip()
        if stripped.startswith("--"):
            continue
        kept.append(line)
    return "\n".join(kept)


def normalize_statements(sql: str) -> list:
    """Split into statements (reusing the runner's quote/dollar-aware splitter),
    drop comment lines, collapse whitespace runs to single spaces. Two SQL
    texts that install the same objects normalize to the same statement list
    regardless of layout."""
    from scripts.migration_runner import split_statements

    out = []
    for raw in split_statements(sql):
        body = _strip_comment_lines(raw)
        collapsed = re.sub(r"\s+", " ", body).strip()
        if collapsed:
            out.append(collapsed)
    return out


@dataclass(frozen=True)
class F2PrefixReport:
    ok: bool
    vacuous: bool  # F.2 has no statements yet — a prefix by construction
    f2_count: int
    first_divergence: int | None  # index of the first mismatch, or None


def f2_prefix_report(f2_statements, stream_statements) -> F2PrefixReport:
    """Is the F.2 statement list a prefix of the stream statement list?

    A vacuous (empty F.2) result is reported as such rather than as a silent
    pass — the caller discloses it, so a gate that is trivially green today
    cannot be mistaken for one that has verified real F.2 output.
    """
    if not f2_statements:
        return F2PrefixReport(ok=True, vacuous=True, f2_count=0, first_divergence=None)
    for i, stmt in enumerate(f2_statements):
        if i >= len(stream_statements) or stmt != stream_statements[i]:
            return F2PrefixReport(
                ok=False,
                vacuous=False,
                f2_count=len(f2_statements),
                first_divergence=i,
            )
    return F2PrefixReport(
        ok=True, vacuous=False, f2_count=len(f2_statements), first_divergence=None
    )


def target_lineage_files(migrations_dir) -> list:
    """The F.2 migration files: numbered files ABOVE the 052 schema-move (the
    target lineage). Uses the runner's own move-marker discovery so it cannot
    drift from the boundary the runner enforces."""
    from scripts.migration_runner import discover_migrations, schema_move_migration

    move = schema_move_migration(migrations_dir)
    return [
        m.path for m in discover_migrations(migrations_dir) if m.version > move.version
    ]


def target_lineage_statements(migrations_dir) -> list:
    """The target lineage's statements, in file order — the F.2 side of every
    comparison against the stream.

    One definition because two callers depend on the SAME list meaning the same
    thing: `test_advertised_ddl` asserts this list is a positional prefix of the
    stream, and the lane's tenancy check slices `stream[: len(this)]` on the
    strength of that fact. Derived twice, the list that was validated and the
    list that drives the slice can diverge the first time the lineage needs any
    filtering — a data-only file, a marker-only file — and only one site would
    get it.
    """
    return [
        stmt
        for path in target_lineage_files(migrations_dir)
        for stmt in normalize_statements(path.read_text())
    ]
