"""The advertised-DDL extractor (plan §0.2 `advertised_ddl_replay`, #753 PR-B).

`02` §0 defines the advertised stream: the literal ```sql fenced blocks (`02`
top-to-bottom, then `07` in file order) MINUS illustrative examples, PLUS the
mechanical expansion of the two §7-DDL policy lists declared by pattern.

Two properties this file pins, both pure text (no database — arm (a)'s replay
is a separate, actor-coupled suite):

1. The extractor parses fenced blocks in file order and hashes each.
2. The committed manifest classifies EVERY block (a bijection with the docs,
   checked both directions) so "illustrative" is a reviewed fact, and a
   plan-doc edit that adds, removes, or changes a block fails the gate until
   the manifest is consciously updated — the F.6-ratchet shape.
"""

import pytest

from pathlib import Path

from scripts.advertised_ddl import (
    DEFAULT_DOCS as REAL_DOCS,
    DEFAULT_MANIFEST as REAL_MANIFEST,
    AdvertisedDDLError,
    Block,
    build_stream,
    expand_policies,
    extract_blocks,
    load_manifest,
    ratchet_report,
)

MIGRATIONS_DIR_PATH = Path(__file__).resolve().parents[2] / "scripts" / "migrations"

# --- fixture docs (controlled; the real-doc counts are asserted separately) ---

FIXTURE_DOC = """\
# A doc

Some prose.

```sql
CREATE TABLE t_one (id INT);
```

More prose introducing a usage example.

```sql
BEGIN;
INSERT INTO t_one VALUES (:bind);
COMMIT;
```

```python
not_sql = True
```

```sql
CREATE TABLE t_two (id INT);
```
"""


class TestExtractBlocks:
    def test_pulls_sql_fences_in_order_ignoring_other_languages(self, tmp_path):
        doc = tmp_path / "d.md"
        doc.write_text(FIXTURE_DOC)

        blocks = extract_blocks(doc)

        assert [b.ordinal for b in blocks] == [1, 2, 3]
        assert blocks[0].sql.strip() == "CREATE TABLE t_one (id INT);"
        assert blocks[1].sql.startswith("BEGIN;")
        assert blocks[2].sql.strip() == "CREATE TABLE t_two (id INT);"
        assert all(isinstance(b, Block) for b in blocks)

    def test_hash_is_stable_and_content_addressed(self, tmp_path):
        doc = tmp_path / "d.md"
        doc.write_text(FIXTURE_DOC)

        first = extract_blocks(doc)
        again = extract_blocks(doc)

        assert [b.sha256 for b in first] == [b.sha256 for b in again]
        assert len({b.sha256 for b in first}) == 3  # distinct content
        assert all(len(b.sha256) == 64 for b in first)


class TestManifestRatchet:
    def _manifest(self, tmp_path, blocks, drop=(), extra=None):
        import json

        entries = [
            {
                "sha256": b.sha256,
                "class": "illustrative" if b.ordinal == 2 else "normative",
                "label": f"block-{b.ordinal}",
                **({"reason": "bind-parameter usage shape"} if b.ordinal == 2 else {}),
            }
            for b in blocks
            if b.ordinal not in drop
        ]
        if extra:
            entries.extend(extra)
        path = tmp_path / "manifest.json"
        path.write_text(json.dumps({"blocks": entries, "policy_expansion": {}}))
        return path

    def test_full_classification_passes_when_bijection_holds(self, tmp_path):
        doc = tmp_path / "d.md"
        doc.write_text(FIXTURE_DOC)
        blocks = extract_blocks(doc)
        manifest = load_manifest(self._manifest(tmp_path, blocks))

        report = ratchet_report([doc], manifest)

        assert report.unclassified == ()
        assert report.orphaned == ()
        assert report.ok is True

    def test_a_new_or_edited_block_is_unclassified_and_fails(self, tmp_path):
        doc = tmp_path / "d.md"
        doc.write_text(FIXTURE_DOC)
        blocks = extract_blocks(doc)
        # manifest built BEFORE a third normative block is added
        manifest = load_manifest(self._manifest(tmp_path, blocks, drop=(3,)))

        report = ratchet_report([doc], manifest)

        assert [u.ordinal for u in report.unclassified] == [3]
        assert report.ok is False

    def test_a_manifest_entry_with_no_matching_block_is_orphaned(self, tmp_path):
        doc = tmp_path / "d.md"
        doc.write_text(FIXTURE_DOC)
        blocks = extract_blocks(doc)
        ghost = {"sha256": "0" * 64, "class": "normative", "label": "ghost"}
        manifest = load_manifest(self._manifest(tmp_path, blocks, extra=[ghost]))

        report = ratchet_report([doc], manifest)

        assert "0" * 64 in report.orphaned
        assert report.ok is False

    def test_build_stream_fails_closed_on_unsatisfied_ratchet(self, tmp_path):
        """build_stream must REFUSE to emit over a ratchet it cannot satisfy —
        a stream built past an unclassified block would silently admit or drop
        DDL, the exact failure the manifest exists to prevent. (Mutation gap:
        ratchet_report is tested directly, but build_stream's own guard was
        not, so a removed guard survived until this landed.)"""
        doc = tmp_path / "d.md"
        doc.write_text(FIXTURE_DOC)
        blocks = extract_blocks(doc)
        # manifest missing the third block → it is unclassified
        manifest = load_manifest(self._manifest(tmp_path, blocks, drop=(3,)))

        with pytest.raises(AdvertisedDDLError, match="unclassified"):
            build_stream([doc], manifest)

    def test_build_stream_fails_closed_on_an_orphaned_manifest_entry(self, tmp_path):
        """The ORPHAN direction of fail-closed, which the unclassified test
        does NOT cover (rajan, #784): build_stream iterates doc-blocks, so an
        unclassified block KeyErrors on its own — but an orphaned manifest
        entry (a hash with no matching block) is never looked up, so WITHOUT
        the ratchet guard build_stream silently returns a normal-looking
        stream. The guard is the only thing that catches it; this proves the
        guard raises rather than emitting a stream built over a stale manifest."""
        doc = tmp_path / "d.md"
        doc.write_text(FIXTURE_DOC)
        blocks = extract_blocks(doc)
        ghost = {"sha256": "0" * 64, "class": "normative", "label": "ghost"}
        manifest = load_manifest(self._manifest(tmp_path, blocks, extra=[ghost]))

        with pytest.raises(AdvertisedDDLError, match="orphaned"):
            build_stream([doc], manifest)

    def test_illustrative_without_a_reason_is_rejected_at_load(self, tmp_path):
        import json

        path = tmp_path / "m.json"
        path.write_text(
            json.dumps(
                {
                    "blocks": [
                        {"sha256": "a" * 64, "class": "illustrative", "label": "x"}
                    ],
                    "policy_expansion": {},
                }
            )
        )
        with pytest.raises(AdvertisedDDLError, match="reason"):
            load_manifest(path)


class TestBuildStream:
    def test_excludes_illustrative_keeps_normative_in_order(self, tmp_path):
        doc = tmp_path / "d.md"
        doc.write_text(FIXTURE_DOC)
        blocks = extract_blocks(doc)
        import json

        entries = [
            {
                "sha256": b.sha256,
                "class": "illustrative" if b.ordinal == 2 else "normative",
                "label": f"b{b.ordinal}",
                **({"reason": "usage"} if b.ordinal == 2 else {}),
            }
            for b in blocks
        ]
        mpath = tmp_path / "m.json"
        mpath.write_text(json.dumps({"blocks": entries, "policy_expansion": {}}))
        manifest = load_manifest(mpath)

        stream = build_stream([doc], manifest)

        assert "t_one" in stream and "t_two" in stream
        assert "BEGIN;" not in stream and ":bind" not in stream
        assert stream.index("t_one") < stream.index("t_two")


class TestPolicyExpansion:
    def test_generates_one_policy_per_table_from_the_template(self):
        generated = expand_policies(
            policy_name="p_tenant",
            tables=["media_items", "post_locks"],
            key_column="workspace_id",
            roles=["svc_ingress", "svc_worker"],
        )

        assert "CREATE POLICY p_tenant ON media_items" in generated
        assert "CREATE POLICY p_tenant ON post_locks" in generated
        assert generated.count("CREATE POLICY") == 2
        assert "workspace_id = NULLIF(current_setting('app.tenant_id'" in generated
        assert "TO svc_ingress, svc_worker" in generated

    def test_open_policy_uses_true_predicate(self):
        generated = expand_policies(
            policy_name="p_user_plane",
            tables=["user_identities"],
            key_column=None,
            roles=["svc_ingress", "svc_worker"],
        )

        assert "USING (true) WITH CHECK (true)" in generated
        assert generated.count("CREATE POLICY") == 1


class TestAgainstTheRealDocs:
    """The counts the doc-map established (subagent inventory): a wrong count
    means the extractor or the manifest drifted from the plan."""

    def test_real_docs_ratchet_is_satisfied(self):
        from scripts.advertised_ddl import DEFAULT_DOCS, DEFAULT_MANIFEST

        manifest = load_manifest(DEFAULT_MANIFEST)
        report = ratchet_report(DEFAULT_DOCS, manifest)

        assert report.ok is True, (
            f"unclassified={[u.label for u in report.unclassified]}"
            f" orphaned={report.orphaned}"
        )

    def test_real_docs_have_the_inventoried_counts(self):
        from scripts.advertised_ddl import DEFAULT_MANIFEST

        manifest = load_manifest(DEFAULT_MANIFEST)
        classes = [e.class_ for e in manifest.blocks]

        # One per `07` increment, each named so the next one has to say so
        # here. The running total, which the prose had fallen a step behind:
        # §8's #883 self-transition guard (061) made it 19, the #942
        # reauth-prompt leg (062) 20, #982's refresh-leg provider guard (063)
        # 21, §10's #1037 memberships door (064) 22, §11's
        # `alert_stranded_sources` kind (065) 23, and §12's #1090 D3 no-media
        # notice marker (066) 24, §13's #1175 bind purpose (067) 25, and §14's
        # #854/#1242 resolver + group-membership doors (068) make it 26.
        assert classes.count("normative") == 26
        assert classes.count("illustrative") == 4

    def test_real_stream_expands_the_fifteen_policies(self):
        from scripts.advertised_ddl import DEFAULT_DOCS, DEFAULT_MANIFEST

        manifest = load_manifest(DEFAULT_MANIFEST)
        stream = build_stream(DEFAULT_DOCS, manifest)

        # 15 generated (13 tenant + 2 user) + the 3 literal exemplars in block 17.
        assert stream.count("CREATE POLICY p_tenant ON ") == 13 + 1
        assert stream.count("CREATE POLICY p_user_plane ON ") == 2 + 1
        for table in ("media_items", "post_intents", "channel_outbox"):
            assert f"CREATE POLICY p_tenant ON {table}" in stream


# --- arm (b): the F.2 prefix-diff (pure text; F.2 is alex's lane) -----------

from scripts.advertised_ddl import (  # noqa: E402
    f2_prefix_report,
    normalize_statements,
    target_lineage_files,
)


class TestNormalizeStatements:
    def test_splits_strips_comments_and_collapses_whitespace(self):
        sql = (
            "-- a header comment\n"
            "CREATE TABLE t (\n    id   INT\n);\n"
            "-- runner:postcondition SELECT true\n"
            "CREATE   INDEX ix ON t (id);"
        )
        stmts = normalize_statements(sql)
        assert stmts == ["CREATE TABLE t ( id INT )", "CREATE INDEX ix ON t (id)"]


class TestF2PrefixReport:
    def _stream_stmts(self):
        return [
            "CREATE TABLE a (id INT)",
            "CREATE TABLE b (id INT)",
            "CREATE TABLE c (id INT)",
        ]

    def test_empty_f2_is_a_vacuous_prefix_and_says_so(self, monkeypatch, tmp_path):
        report = f2_prefix_report([], self._stream_stmts())
        assert report.ok is True
        assert report.vacuous is True
        assert report.f2_count == 0

    def test_matching_prefix_holds_and_is_not_vacuous(self):
        f2 = ["CREATE TABLE a (id INT)", "CREATE TABLE b (id INT)"]
        report = f2_prefix_report(f2, self._stream_stmts())
        assert report.ok is True
        assert report.vacuous is False
        assert report.f2_count == 2

    def test_a_diverging_statement_fails_and_names_the_position(self):
        f2 = ["CREATE TABLE a (id INT)", "CREATE TABLE WRONG (id INT)"]
        report = f2_prefix_report(f2, self._stream_stmts())
        assert report.ok is False
        assert report.first_divergence == 1  # 0-based index of the mismatch

    def test_f2_longer_than_the_stream_fails(self):
        f2 = self._stream_stmts() + ["CREATE TABLE extra (id INT)"]
        report = f2_prefix_report(f2, self._stream_stmts())
        assert report.ok is False
        assert report.first_divergence == 3


class TestF2AgainstReality:
    def test_the_target_lineage_head_is_the_two_shared_functions(self):
        """ARM (b) IS LOAD-BEARING FROM HERE (#806, migration 052).

        This replaces `test_target_lineage_is_empty_today_disclosed`, which
        asserted the target lineage was empty and was written to go red the
        moment a file landed. 052 is that file, so the disclosure is retired
        by giving arm (b) the real assertion it was holding the place for.

        The expectation is derived from the STREAM — the plan documents plus
        the manifest — and not from the migration file, which is the artifact
        under test. Deriving it from the file would make this pass for any
        content whatsoever.
        """
        files = target_lineage_files(MIGRATIONS_DIR_PATH)
        assert files, (
            "the target lineage is empty — 052 is the head of the advertised"
            " stream and must be the first file above the 051 move"
        )

        manifest = load_manifest(REAL_MANIFEST)
        expected = normalize_statements(build_stream(REAL_DOCS, manifest))[:2]
        head = normalize_statements(files[0].read_text())

        assert head == expected, (
            f"{files[0].name} is not the advertised stream's opening"
            " statements — arm (b) requires an ordered prefix"
        )

    def test_the_head_is_the_shared_functions_and_carries_no_table(self):
        """The plan-level fact the prefix diff cannot state on its own.

        A prefix diff only says "these match the stream's opening". It stays
        green if the plan and the migration are changed together. `02` §0 says
        the head of the target lineage is the two SHARED FUNCTIONS — asserted
        by name here, independently of the diff.

        The no-table half is what makes this increment independent of the open
        question about whether a table may land before its policy: that
        question governs tables, and a file with none is not subject to it.
        """
        head = normalize_statements(
            target_lineage_files(MIGRATIONS_DIR_PATH)[0].read_text()
        )
        joined = " ".join(head)

        assert "CREATE FUNCTION trg_touch_updated_at()" in joined
        assert "CREATE FUNCTION fn_safe_tz(p_tz text)" in joined
        assert "CREATE TABLE" not in joined, (
            "a table landed in the shared-functions increment — that puts it"
            " under the table-before-policy question this file is exempt from"
        )

    def test_the_wired_prefix_holds_against_the_real_stream(self):
        """The wiring itself, exercised end-to-end against the real docs +
        manifest: build the stream, normalize the target lineage, diff.

        `ok is True` was the whole assertion while the lineage was empty, and
        an empty prefix is vacuously ok — so this test passed without
        comparing anything, by design and disclosed as such. Now that 052 has
        landed, NON-VACUITY is asserted alongside it. Without that line,
        deleting every target-lineage migration would turn this green again
        rather than red, which is the failure the disclosure existed to
        prevent and would otherwise have been reintroduced silently the moment
        it was removed.
        """
        manifest = load_manifest(REAL_MANIFEST)
        stream = build_stream(REAL_DOCS, manifest)
        f2 = [
            s
            for f in target_lineage_files(MIGRATIONS_DIR_PATH)
            for s in normalize_statements(f.read_text())
        ]
        report = f2_prefix_report(f2, normalize_statements(stream))
        assert report.ok is True
        assert report.vacuous is False, (
            "the prefix is vacuous — the target lineage carries no statements,"
            " so this test compared nothing"
        )
        assert report.f2_count == len(f2) > 0
