"""The SQL lexer every text scan of a migration shares (#1406).

Three scans read SQL as text and each had to know where a quoted literal
starts and ends: the runner's statement splitter, the advertised-DDL
normalizer, and the tenancy gate's two structural reads. Each knew it
differently, and each gap let a string literal change a gate's verdict. The
lexer is now the one place that answers, so it is pinned here against
PostgreSQL's own behaviour rather than against a reading of its scanner: every
case that turns on a subtle rule — escape strings, their continuation across a
line break, nested block comments, dollar tags — was first put to a live server
(PostgreSQL 15, `standard_conforming_strings` on), and `TestPostgresAgrees`
keeps asking one.
"""

from functools import partial

import pytest

from scripts.migration_runner import (
    MIGRATIONS_DIR,
    discover_migrations,
    split_statements,
)
from scripts.sql_lexer import (
    BLOCK_COMMENT,
    CODE,
    DOLLAR,
    ESCAPE_STRING,
    IDENTIFIER,
    LINE_COMMENT,
    STRING,
    Segment,
    name_of,
    segments,
)
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
        from its segments gets the statement back. Held over every migration
        file, so the corpus's own forms are covered, not only the cases above."""
        for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
            sql = path.read_text()
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
        """#1406's case: the quote-blind splitter ended the literal at `\\'`,
        opened a phantom one at the real close, and read the whole rest of the
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
