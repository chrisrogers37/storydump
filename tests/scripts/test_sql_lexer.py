"""The SQL lexer every text scan of SQL shares.

The runner's statement splitter and the statement normalizer read SQL as text,
and each has to know where a quoted literal, a quoted identifier and a comment
start and end: a gap there lets a string literal change what they cut or
compare. The lexer is the one place that answers, so it is pinned here against
PostgreSQL's own behaviour rather than against a reading of its scanner: every
case that turns on a subtle rule — escape strings, their continuation across a
line break, nested block comments, dollar tags — is what a live server
(PostgreSQL 15, `standard_conforming_strings` on) does, and `TestPostgresAgrees`
keeps asking one.
"""

from functools import partial

import pytest

from scripts.migration_runner import discover_migrations, split_statements
from scripts.sql_lexer import (
    BLOCK_COMMENT,
    CODE,
    DOLLAR,
    ESCAPE_STRING,
    IDENTIFIER,
    LINE_COMMENT,
    STRING,
    QuotesWouldMove,
    Segment,
    name_of,
    normalize_statements,
    segments,
)
from src.models.target import ddl
from tests.scripts.conftest import write_migration


C, S, E, I, D, L, B = (  # noqa: E741 - each reads as the kind it builds
    partial(Segment, kind)
    for kind in (
        CODE,
        STRING,
        ESCAPE_STRING,
        IDENTIFIER,
        DOLLAR,
        LINE_COMMENT,
        BLOCK_COMMENT,
    )
)


class TestSegments:
    """Each case is written so that one wrong boundary changes the segment
    list, which is what every consumer reads."""

    CASES = {
        # '…' ends at a lone quote; a doubled quote stays inside, and with
        # standard_conforming_strings on a backslash is plain text.
        "comma_in_a_string": ("a 'x,y' b", [C("a "), S("'x,y'"), C(" b")]),
        "doubled_quote_stays_inside": ("'it''s' b", [S("'it''s'"), C(" b")]),
        "backslash_is_text_in_a_standard_string": (
            r"'a\' , b",
            [S(r"'a\'"), C(" , b")],
        ),
        # The prefixed forms end by the same rule.
        "bit_string": ("B'1010' x", [S("B'1010'"), C(" x")]),
        "hex_string": ("X'1F' x", [S("X'1F'"), C(" x")]),
        "national_string": ("N'x' y", [S("N'x'"), C(" y")]),
        "unicode_string": ("U&'x''y' z", [S("U&'x''y'"), C(" z")]),
        # E'…': a backslash escapes the next character, the quote included,
        # and a doubled quote still stays inside.
        "escape_string_backslash_quote": (r"E'it\'s' x", [E(r"E'it\'s'"), C(" x")]),
        "escape_string_lower_case_prefix": (r"e'\'' x", [E(r"e'\''"), C(" x")]),
        "escape_string_doubled_then_escaped": (
            r"E'a''\'b' x",
            [E(r"E'a''\'b'"), C(" x")],
        ),
        "escape_string_escaped_backslash_then_close": (
            r"E'a\\' , b",
            [E(r"E'a\\'"), C(" , b")],
        ),
        # A literal followed by a line break (comments allowed) and another
        # quote CONTINUES — and an escape string's continuation keeps the
        # escape rules, so the whole run is one escape string.
        "escape_string_continues_across_a_line_break_and_a_comment": (
            "E'a\\'' -- c'mon\n'b\\'c' x",
            [E("E'a\\'' -- c'mon\n'b\\'c'"), C(" x")],
        ),
        "escape_string_does_not_continue_on_one_line": (
            r"E'a' 'b\' x",
            [E("E'a'"), C(" "), S(r"'b\'"), C(" x")],
        ),
        # Only a lone E opens one: an identifier ending in e run into a quote
        # is a typed literal, `name 'a\'`.
        "identifier_ending_in_e_is_not_an_escape_prefix": (
            r"name'a\' , b",
            [C("name"), S(r"'a\'"), C(" , b")],
        ),
        # "…" identifiers.
        "quoted_identifier": ('"a,b" c', [I('"a,b"'), C(" c")]),
        "doubled_quote_in_an_identifier": ('"a""b" c', [I('"a""b"'), C(" c")]),
        "unicode_identifier": ('U&"x" y', [I('U&"x"'), C(" y")]),
        # $tag$…$tag$: the tag may carry digits after its first character, and
        # only the SAME tag closes the body.
        "dollar_quote": ("$$a;b$$ c", [D("$$a;b$$"), C(" c")]),
        "dollar_tag_with_a_digit": ("$a1$x;y$a1$ z", [D("$a1$x;y$a1$"), C(" z")]),
        "another_tag_inside_does_not_close": (
            "$a$ $b$ $a$ z",
            [D("$a$ $b$ $a$"), C(" z")],
        ),
        # A `$` inside an identifier belongs to the identifier, and `$1` is a
        # parameter: neither opens a body.
        "dollar_inside_an_identifier_is_code": ("a$b$ , c", [C("a$b$ , c")]),
        "positional_parameter_is_code": ("$1 , $2", [C("$1 , $2")]),
        # -- runs to the end of its line (a carriage return ends it too), and
        # starts even inside an operator.
        "line_comment_holds_a_quote": (
            "a -- it's\nb",
            [C("a "), L("-- it's"), C("\nb")],
        ),
        "line_comment_ends_at_a_carriage_return": (
            "a -- x\r+ 1",
            [C("a "), L("-- x"), C("\r+ 1")],
        ),
        "comment_starts_inside_an_operator": (
            "1+-- c\n2",
            [C("1+"), L("-- c"), C("\n2")],
        ),
        "line_comment_at_the_end_of_the_text": ("a -- b", [C("a "), L("-- b")]),
        # /* … */ nests.
        "block_comment_holds_a_quote": (
            "a /* it's; */ b",
            [C("a "), B("/* it's; */"), C(" b")],
        ),
        "block_comments_nest": ("/* /* */ x */ 2", [B("/* /* */ x */"), C(" 2")]),
        "empty_block_comment": ("/**/ 1", [B("/**/"), C(" 1")]),
        "stars_before_the_close": ("/* **/ 1", [B("/* **/"), C(" 1")]),
        # Inside a literal, the markers of every other form are text.
        "markers_in_a_string": ("'-- /* $$' x", [S("'-- /* $$'"), C(" x")]),
        "markers_in_a_dollar_body": ("$$ -- ' /* $$ x", [D("$$ -- ' /* $$"), C(" x")]),
        # Text that ends inside a form says so: every consumer that refuses
        # unbounded text reads this flag, so a form the input never closes must
        # not come back looking closed. (A line comment is closed by the end.)
        "unclosed_string": ("a 'bc", [C("a "), S("'bc", closed=False)]),
        "unclosed_escape_string": (r"E'a\'", [E(r"E'a\'", closed=False)]),
        "unclosed_identifier": ('"ab', [I('"ab', closed=False)]),
        "unclosed_dollar_quote": ("$$ab", [D("$$ab", closed=False)]),
        "unclosed_block_comment": ("/* ab", [B("/* ab", closed=False)]),
        "unclosed_nested_block_comment": (
            "/* /* */ ab",
            [B("/* /* */ ab", closed=False)],
        ),
    }

    @pytest.mark.parametrize("name", sorted(CASES))
    def test_each_form_ends_where_postgres_ends_it(self, name):
        sql, expected = self.CASES[name]
        assert segments(sql) == expected

    def test_the_segments_are_the_text(self):
        """Nothing is dropped or doubled: a consumer that rebuilds a statement
        from its segments gets the statement back. Held over every SQL file of
        the schema code, so the schema's own forms are covered, not only the
        cases above."""
        paths = sorted(ddl.HERE.glob("*.sql"))
        assert paths, f"positive control: no SQL files under {ddl.HERE}"
        for path in paths:
            sql = path.read_text(encoding="utf-8")
            assert "".join(s.text for s in segments(sql)) == sql, path.name


class TestNames:
    """`name_of` is what an identifier token stands for, as a live server resolves
    it: an unquoted word folds its ASCII letters and nothing else, a quoted
    name is exact, and a `U&"…"` name can spell anything through its escapes —
    `U&"\\0077orkspace_id"` IS `workspace_id` — so it is not resolved at all."""

    CASES = {
        "a_word_folds_its_ascii_letters": ("WORKSPACE_ID", "workspace_id"),
        "a_kelvin_sign_is_not_a_k": ("WOR\u212aSPACE_ID", "wor\u212aspace_id"),
        "a_quoted_name_is_exact": ('"Workspace_Id"', "Workspace_Id"),
        "a_doubled_quote_in_a_quoted_name": ('"a""b"', 'a"b'),
        "unicode_escapes_are_not_resolved": ('U&"\\0077orkspace_id"', None),
    }

    @pytest.mark.parametrize("case", sorted(CASES))
    def test_a_token_names_what_postgres_says(self, case):
        token, expected = self.CASES[case]
        assert name_of(token) == expected


class TestTheRunnerSplitsOnTheLexer:
    """`split_statements` cuts at a `;` in code and nowhere else. It lives in
    the runner, whose predeploy applies what it cuts."""

    def test_an_escaped_quote_does_not_end_the_literal(self):
        """#1406's case: a quote-blind splitter ends the literal at `\\'`,
        opens a phantom one at the real close, and reads the whole rest of the
        file as one statement."""
        sql = "CREATE TABLE t (a text DEFAULT E'it\\'s here');\nCREATE TABLE u (b int);"
        assert split_statements(sql) == [
            "CREATE TABLE t (a text DEFAULT E'it\\'s here')",
            "\nCREATE TABLE u (b int)",
        ]

    def test_a_semicolon_in_text_does_not_split(self):
        sql = "SELECT 'a;b', \"c;d\", $$e;f$$ /* g;h */ -- i;j\n;SELECT 2"
        assert split_statements(sql) == [
            "SELECT 'a;b', \"c;d\", $$e;f$$ /* g;h */ -- i;j\n",
            "SELECT 2",
        ]

    @pytest.mark.parametrize(
        "begin", ["BEGIN -- the file's own", "/* its own */ BEGIN"]
    )
    def test_a_commented_begin_still_marks_the_file_self_managed(self, tmp_path, begin):
        """The runner reads a file's execution mode off its statements, and a
        BEGIN with a comment beside it is still the file's own transaction:
        wrapping the file in a second one would nest it."""
        write_migration(tmp_path, 1, f"{begin}\n;\nCREATE TABLE a (id int);\nCOMMIT;")
        [migration] = discover_migrations(tmp_path)
        assert migration.execution_mode == "self-managed"

    def test_comments_stay_in_the_statement_they_sit_in(self):
        """The runner reads its markers and execution mode from the text it is
        handed, comments included; only a fragment of nothing BUT comments is
        dropped, a block comment as much as a line one."""
        sql = "-- head\nSELECT 1 /* one */;\n/* only a comment */;\n-- tail\n"
        assert split_statements(sql) == ["-- head\nSELECT 1 /* one */"]


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

    def test_a_literal_line_that_starts_with_dashes_is_kept(self):
        """A literal's continuation line that starts with `--` is text, not a
        comment. Dropped, it would take the clauses on it with it: here a DROP
        COLUMN workspace_id, which PostgreSQL runs."""
        sql = (
            "ALTER TABLE t ADD COLUMN foo text DEFAULT 'abc\n"
            "--x', DROP COLUMN workspace_id, ALTER COLUMN foo SET DEFAULT 'y\n"
            "z';"
        )
        assert normalize_statements(sql) == [
            "ALTER TABLE t ADD COLUMN foo text DEFAULT 'abc --x', DROP COLUMN"
            " workspace_id, ALTER COLUMN foo SET DEFAULT 'y z'"
        ]

    def test_a_comment_after_code_goes_too(self):
        """Normalized text is one line, so a comment left in it has no end: its
        `--` would run over the rest of the statement for any scan that reads
        quotes. Every comment is dropped — trailing, block — never just the
        lines that hold nothing else."""
        sql = (
            "CREATE TABLE t (\n"
            "    id uuid, -- the row's id\n"
            "    workspace_id uuid /* the tenant */\n"
            ");"
        )
        assert normalize_statements(sql) == [
            "CREATE TABLE t ( id uuid, workspace_id uuid )"
        ]

    def test_a_line_in_a_function_body_is_kept(self):
        """A dollar-quoted body is a literal: PostgreSQL stores it verbatim
        (`pg_proc.prosrc`), its comment lines included, so a comparison of
        normalized bodies has to see a change to one."""
        sql = (
            "CREATE FUNCTION f() RETURNS int LANGUAGE sql AS $$\n"
            "    -- one, always\n"
            "    SELECT 1\n"
            "$$;"
        )
        assert normalize_statements(sql) == [
            "CREATE FUNCTION f() RETURNS int LANGUAGE sql AS $$ -- one, always SELECT 1 $$"
        ]

    def test_a_comment_still_separates_what_it_sat_between(self):
        """PostgreSQL reads a comment as whitespace, so `a/* x */b` is two
        names; removed without a space, they would read as one."""
        assert normalize_statements("SELECT a/* x */b;") == ["SELECT a b"]

    def test_layout_around_a_literal_is_not_a_moved_quote(self):
        """The refusal below compares what the quotes bound, not the blanks
        between them: a statement that ends in a literal loses its trailing
        line break to normalization and must still normalize."""
        assert normalize_statements("COMMENT ON TABLE t IS 'x'\n;") == [
            "COMMENT ON TABLE t IS 'x'"
        ]

    def test_comment_markers_inside_a_literal_are_text(self):
        sql = "SELECT '-- not a comment /* nor this */' AS v;"
        assert normalize_statements(sql) == [
            "SELECT '-- not a comment /* nor this */' AS v"
        ]

    def test_text_whose_quotes_would_move_is_refused(self):
        """An escape string continued across a line break keeps its escape
        rules only while the break is there: collapsed onto one line, the
        continuation becomes a standard string and `\\'` ends it. No normalized
        form keeps the quotes where PostgreSQL has them, so the statement is
        refused rather than handed to a scan that trusts its quotes."""
        sql = "SELECT E'a\\''\n'b\\'c';"
        with pytest.raises(QuotesWouldMove, match="quotes"):
            normalize_statements(sql)


@pytest.mark.integration
class TestPostgresAgrees:
    """The cases above were read off a live server once; these keep asking it.

    Each probe is several statements. Every statement the splitter cuts must
    run ALONE as a subquery — a statement cut short, or two run together, is a
    syntax error there — and the count must be the one PostgreSQL sees."""

    PROBES = {
        "escaped_quote": ("SELECT E'it\\'s; here' AS v; SELECT 2 AS v", 2),
        "escape_string_continuation": (
            "SELECT E'a\\'' -- c'mon; x\n'b\\';c' AS v; SELECT 2 AS v",
            2,
        ),
        "dollar_tag_with_a_digit": ("SELECT $a1$x;y$a1$ AS v; SELECT 2 AS v", 2),
        "block_comments": (
            "SELECT /* ; it's */ 1 AS v; SELECT /* /* ; */ ; */ 2 AS v",
            2,
        ),
        "dollar_inside_an_identifier": ("SELECT 1 AS a$b$; SELECT 2 AS a$b$", 2),
        "typed_literal": ("SELECT name'a\\' AS v; SELECT 2 AS v", 2),
        "quote_in_a_line_comment": ("SELECT 1 AS v -- it's; not\n; SELECT 2 AS v", 2),
        "backslash_in_a_standard_string": ("SELECT 'a\\' AS v; SELECT 2 AS v", 2),
    }

    @pytest.mark.parametrize("name", sorted(PROBES))
    def test_every_statement_cut_is_one_postgres_runs_alone(self, admin_conn, name):
        sql, count = self.PROBES[name]
        statements = split_statements(sql)
        assert len(statements) == count, statements
        with admin_conn.cursor() as cur:
            for statement in statements:
                cur.execute(f"SELECT * FROM ({statement}\n) AS probe")
