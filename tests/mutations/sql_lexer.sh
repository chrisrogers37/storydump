#!/bin/zsh
# Mutation battery for the SQL lexer and the three scans that read migrations through it (#1406): the
# runner's splitter, the advertised-DDL normalizer, and the tenancy gate's two text reads. Each behaviour
# has one named mutation that must make its test FAIL ("killed") — and must PASS on the clean tree first,
# or the verdict is BASELINE RED; a selector that selects nothing is NO TEST SELECTED, never a kill. Files
# are restored from the COMMITTED tree after each, so commit first. No database: every selected test is
# pure text, and the recipe deselects the PostgreSQL probes a `-k` name would otherwise also pick up.
# `STORYDUMP_ROOT` points the battery at a worktree.
set -u
ROOT=${STORYDUMP_ROOT:-/Users/chris/Projects/storydump}
PY=${STORYDUMP_PY:-/Users/chris/Projects/storydump/.venv/bin/python}
cd "$ROOT" || exit 2
KEY=$($PY -c "from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())")
UNIT="env -u DB_HOST -u DB_USER -u DB_PASSWORD -u DB_NAME -u TEST_DB_NAME -u REQUIRE_TEST_DATABASE -u TELEGRAM_BOT_TOKEN -u TELEGRAM_CHANNEL_ID -u ADMIN_TELEGRAM_CHAT_ID -u WORKER_IMPL -u TARGET_DATABASE_URL DB_PORT=65432 PYTHONDONTWRITEBYTECODE=1 ENCRYPTION_KEY=$KEY $PY -m pytest -q -p no:cacheprovider --no-cov -x -m 'not integration'"
# One log per run, never a fixed path: two batteries on one host must not read each other's verdicts.
LOG=$(mktemp) || exit 2
trap 'rm -f "$LOG"' EXIT
RAN=0
EXPECTED=$(grep -cE '^check "' "$0")
verdict() {
  local name=$1 rc=$2
  local summary; summary=$(grep -E '^=+ .*(passed|failed|error|deselected|no tests ran)' "$LOG" | tail -1)
  if grep -qE '/ 0 selected|no tests ran' "$LOG"; then echo "NO TEST SELECTED (bad): $name  [$summary]"
  elif [ $rc -eq 0 ]; then echo "SURVIVED (bad): $name  [$summary]"
  elif ! grep -qE '^=+ .*[0-9]+ failed' "$LOG"; then echo "KILLED BY ERROR (check): $name  [$summary]"
  else echo "killed: $name  [$summary]"; fi
}
mutate() {
  OLD="$2" NEW="$3" $PY - "$1" <<'PY'
import os, sys, pathlib
p = pathlib.Path(sys.argv[1]); s = p.read_text(); old = os.environ["OLD"]; new = os.environ["NEW"]
if s.count(old) != 1:
    print(f"MUTATION NOT APPLIED ({s.count(old)} matches)"); sys.exit(3)
p.write_text(s.replace(old, new, 1))
PY
}
check() {  # name file old new test-selector
  local name=$1 file=$2 old=$3 new=$4 sel=$5
  if [ -n "${ONLY:-}" ] && ! [[ "$name" =~ $ONLY ]]; then return; fi
  RAN=$((RAN + 1))
  eval "$UNIT $sel" > "$LOG" 2>&1; local base=$?
  if grep -qE '/ 0 selected|no tests ran' "$LOG"; then echo "NO TEST SELECTED (bad): $name  [$(grep -E '^=+ .*(selected|no tests ran)' "$LOG" | tail -1)]"; return; fi
  if [ $base -ne 0 ]; then echo "BASELINE RED (bad): $name  [$(grep -E '^=+ .*(passed|failed|error)' "$LOG" | tail -1)]"; return; fi
  if ! mutate "$file" "$old" "$new"; then echo "MUTATION NOT APPLIED: $name"; git checkout -- "$file"; return; fi
  rm -rf "$(dirname "$file")/__pycache__"
  eval "$UNIT $sel" > "$LOG" 2>&1; local rc=$?
  verdict "$name" $rc
  cd "$ROOT" && git checkout -- "$file"
}

S=scripts/sql_lexer.py
R=scripts/migration_runner.py
N=scripts/advertised_ddl.py
T=scripts/tenancy_gate.py
L="tests/scripts/test_sql_lexer.py"
A="tests/scripts/test_advertised_ddl.py"
G="tests/scripts/test_tenancy_gate.py"

# The lexer: where each form ends, as PostgreSQL ends it.
E_PREFIX='    if prefix in ("E", "e") and'
check "a backslash escapes nothing in an escape string" $S '        if ch == "\\":
            i += 2' '        if ch == "\\":
            i += 1' "$L -k escape_string_backslash_quote"
check "a doubled quote ends an escape string" $S '            i += 2
        else:
            continued' '            return i + 1, True
        else:
            continued' "$L -k escape_string_doubled_then_escaped"
check "an escape string stops at a line break" $S '            continued = _CONTINUATION.match(sql, i + 1)' '            continued = None' "$L -k escape_string_continues"
check "E opens no escape string" $S "$E_PREFIX" '    if prefix in () and' "$L -k escape_string_backslash_quote"
check "a word is read a character at a time" $S '    return None, after, True' '    return None, i + 1, True' "$L -k 'identifier_ending_in_e or dollar_inside_an_identifier'"
check "a doubled quote ends a string" $S '        if not sql.startswith(quote, close + 1):' '        if True:' "$L -k doubled_quote_stays_inside"
check "a dollar tag cannot carry a digit" $S '    r"\$(?:[A-Za-z_\u0080-\U0010ffff][A-Za-z0-9_\u0080-\U0010ffff]*)?\$"' '    r"\$(?:[A-Za-z_\u0080-\U0010ffff][A-Za-z_\u0080-\U0010ffff]*)?\$"' "$L -k dollar_tag_with_a_digit"
check "any doubled dollar closes a body" $S '        close = sql.find(tag.group(), tag.end())' '        close = sql.find("$$", tag.end())' "$L -k another_tag_inside_does_not_close"
check "a carriage return does not end a line comment" $S '_LINE_END = re.compile(r"[\n\r]")' '_LINE_END = re.compile(r"\n")' "$L -k carriage_return"
check "block comments do not nest" $S '        elif sql.startswith("/*", i):
            depth += 1' '        elif sql.startswith("/*", i):
            depth += 0' "$L -k block_comments_nest"
check "an unterminated string reads as closed" $S '            return len(sql), False
        if not sql.startswith' '            return len(sql), True
        if not sql.startswith' "$L -k unclosed"
check "the code before a form is dropped" $S '            if code_start < i:
                out.append' '            if False:
                out.append' "$L -k segments_are_the_text"

# Names: what an identifier token names, as PostgreSQL resolves it.
FOLD='    return token.translate(_ASCII_LOWER)'
UNICODE_NAME='    if token[:2] in ("U&", "u&"):
        return None'
check "a word keeps its case" $S "$FOLD" '    return token' "$L -k a_word_folds_its_ascii_letters"
check "a word keeps its case, through the gate" $S "$FOLD" '    return token' "$G -k 'a_column_named_the_key and upper_case_column'"
check "folding reaches past ASCII" $S "$FOLD" '    return token.lower()' "$L -k a_kelvin_sign_is_not_a_k"
check "a quoted name keeps its quotes" $S '        return token[1:-1].replace' '        return token.replace' "$L -k a_quoted_name_is_exact"
check "a U& name resolves" $S "$UNICODE_NAME" '    if False:
        return None' "$L -k unicode_escapes_are_not_resolved"
check "a U& name resolves, through the gate" $S "$UNICODE_NAME" '    if False:
        return None' "$G -k unicode_escaped_identifier"

# The runner: a `;` in code and nowhere else, and a BEGIN read as its code.
check "the splitter reads E'…' as a standard string" $S "$E_PREFIX" '    if prefix in () and' "$L -k escaped_quote_does_not_end_the_literal"
check "a semicolon in text splits" $R '        parts = seg.text.split(";") if seg.kind == CODE else [seg.text]' '        parts = seg.text.split(";")' "$L -k semicolon_in_text"
check "a fragment of comments is a statement" $R '            has_code = has_code or (seg.kind not in COMMENTS and bool(part.strip()))' '            has_code = has_code or bool(part.strip())' "$L -k comments_stay"
check "a commented BEGIN is not the file's own" $R '            " " if s.kind in COMMENTS else s.text for s in segments(statement)' '            s.text for s in segments(statement)' "$L -k commented_begin"

# The normalizer: every comment goes, every literal stays.
COMMENT_RULE='        text = "".join(" " if s.kind in COMMENTS else s.text for s in segments(raw))'
LINE_RULE='        text = "\n".join(l for l in raw.split("\n") if not l.strip().startswith("--"))'
BODY_RULE='        text = "".join(" " if s.kind in COMMENTS else "\n".join(l for l in s.text.split("\n") if not l.strip().startswith("--")) if s.kind == "dollar" else s.text for s in segments(raw))'
check "the quote-blind line rule is back" $N "$COMMENT_RULE" "$LINE_RULE" "$A -k literal_line_that_starts_with_dashes"
check "the quote-blind line rule is back, through the gate" $N "$COMMENT_RULE" "$LINE_RULE" "$G -k literal_lines_dropped_in_a_pair"
check "the quote-blind line rule is back, unpinned" $N "$COMMENT_RULE" "$LINE_RULE" "$A -k TestWhatTheCommentRuleChangedInTheStream"
check "a trailing comment is kept" $N "$COMMENT_RULE" '        text = "".join(s.text for s in segments(raw))' "$A -k comment_after_code"
check "a trailing comment is kept, unpinned" $N "$COMMENT_RULE" '        text = "".join(s.text for s in segments(raw))' "$A -k TestWhatTheCommentRuleChangedInTheStream"
check "a function body loses its -- lines" $N "$COMMENT_RULE" "$BODY_RULE" "$A -k line_in_a_function_body"
check "a function body loses its -- lines, unpinned" $N "$COMMENT_RULE" "$BODY_RULE" "$A -k TestWhatTheCommentRuleChangedInTheStream"
check "a comment is removed without a space" $N "$COMMENT_RULE" '        text = "".join("" if s.kind in COMMENTS else s.text for s in segments(raw))' "$A -k separates_what_it_sat_between"
check "normalization moves a quote silently" $N '        if _shape(collapsed) != _shape(text):' '        if False:' "$A -k quotes_would_move"
check "layout reads as a moved quote" $N '        (s.kind, s.closed) for s in segments(sql) if s.kind != CODE or s.text.strip()' '        (s.kind, s.closed) for s in segments(sql)' "$A -k layout_around_a_literal"

# The tenancy gate: what its two reads refuse, and what the key is.
U='_UNREAD = frozenset({ESCAPE_STRING, DOLLAR}) | COMMENTS'
NAMES='            names += [name_of(word) for word in WORD.findall(seg.text)]'
ADD_NAME='    rf"(?!IF NOT EXISTS\b)({WORD.pattern})"'
check "the gate reads through an escape string" $T "$U" '_UNREAD = frozenset({DOLLAR}) | COMMENTS' "$G -k 'refused_alone and escape_string'"
check "the gate reads through a dollar body" $T "$U" '_UNREAD = frozenset({ESCAPE_STRING}) | COMMENTS' "$G -k 'refused_alone and dollar_quoted'"
check "the gate reads through a block comment" $T "$U" '_UNREAD = frozenset({ESCAPE_STRING, DOLLAR}) | {"line_comment"}' "$G -k 'refused_alone and block_comment'"
check "the gate reads through a line comment" $T "$U" '_UNREAD = frozenset({ESCAPE_STRING, DOLLAR}) | {"block_comment"}' "$G -k 'cannot_bound_is_refused and comment'"
check "the guard reads through an unclosed quote" $T '        if seg.kind in _UNREAD or not seg.closed:
            return True' '        if seg.kind in _UNREAD:
            return True' "$G -k 'behind_quoting_is_refused and unterminated_literal'"
check "the guard counts parens inside literals" $T '        if seg.kind != CODE:
            continue
' '' "$G -k 'behind_quoting_is_refused and quoted_parens_balanced'"
check "the key is read inside a literal" $T '        elif seg.kind == CODE:
            names +=' '        elif seg.kind in ("code", "string"):
            names +=' "$G -k named_only_in_a_default"
check "a quoted name is not read" $T '        elif seg.kind == IDENTIFIER:
            names.append(' '        elif False:
            names.append(' "$G -k quoted_column"
check "a name splits at a dollar" $T "$NAMES" '            names += [name_of(word) for word in re.findall(r"\w+", seg.text)]' "$G -k 'anything_else_that_spells and a_longer_identifier'"
check "the name read goes through an unclosed quote" $T '        if seg.kind in _UNREAD or not seg.closed:
            names.append(None)' '        if seg.kind in _UNREAD:
            names.append(None)' "$G -k 'cannot_bound_is_refused and unterminated_literal'"
check "a name that cannot be read counts as absent" $T '    if None in names:' '    if False:' "$G -k cannot_bound_is_refused"
check "an added name keeps its case" $T '            if name_of(m.group(2)) == TENANT_KEY and m.group(1) in sig:' '            if m.group(2) == TENANT_KEY and m.group(1) in sig:' "$G -k 'added_column_is_read and upper_case_column'"
check "IF NOT EXISTS stops an added name being read" $T '    r"ALTER TABLE (?:public\.)?(\w+) ADD COLUMN (?:IF NOT EXISTS )?"' '    r"ALTER TABLE (?:public\.)?(\w+) ADD COLUMN "' "$G -k 'added_column_is_read and if_not_exists'"
check "a backtrack reads IF as the added name" $T "$ADD_NAME" '    rf"({WORD.pattern})"' "$G -k a_quoted_name_after_if_not_exists"
check "an added name splits at a dollar" $T "$ADD_NAME" '    rf"(?!IF NOT EXISTS\b)(\w+)"' "$G -k 'added_column_is_read and a_longer_identifier'"

echo "ran $RAN of $EXPECTED mutations${ONLY:+ (ONLY=$ONLY)}"
