"""Tenancy gate (F.2.0, #746) — the check that can see RLS.

`schema_parity` compares the runner-replayed schema against what
``Base.metadata.create_all`` builds from the models. That comparison cannot
carry RLS, because the models side cannot produce it: measured on this repo,
``create_all`` emits 24 foreign keys and **zero** policies, zero RLS-enabled
tables, zero triggers, zero functions. Adding policies to the parity signature
would not gate them — it would make parity permanently red.

So tenancy is an **invariant over the replayed schema alone**, not a comparison
against the models: every workspace-keyed table is RLS-enabled and carries at
least one policy. Foreign keys, which both sides do emit, belong in the parity
signature instead and are added there rather than here.

That invariant is this module's original job and still its main one. It now also
exports the static half of a **different** comparison — `expected_tenancy`
derives the same signature shape from plan text, so a caller partway through the
F.2 lineage can ask "does the replay match what the plan implies *at this
point*" during the window where the invariant above is legitimately false (the
ratified stream creates every table before enabling RLS on any of them). Both
producers build their entries through `_tenancy_entry`, so the two shapes cannot
drift apart.

Why this exists at all: `02` §7 prints a table's `ENABLE ROW LEVEL SECURITY`
and its policies in the same replay — tables are *born* tenant-scoped. Nothing
in this repository could observe that. A migration creating a workspace-keyed
table and omitting its policy passed every existing check, which made FC-1 an
assertion in a document rather than an enforced property.

Standalone stdlib + psycopg2, matching `migration_runner` and `schema_parity`:
the gate must be runnable in the same predeploy context, importing nothing from
``src``.
"""

from __future__ import annotations

import re

import psycopg2

from scripts.sql_lexer import (
    CODE,
    COMMENTS,
    DOLLAR,
    ESCAPE_STRING,
    IDENTIFIER,
    WORD,
    name_of,
    segments,
)

#: The column that marks a table as belonging to a tenant (`02` §1, FC-1).
TENANT_KEY = "workspace_id"

#: `workspaces` IS the tenant — `02` §7-DDL Class 1: "workspaces keys on id
#: (it IS the tenant)". It carries the tenant policy pair like any other
#: tenant-plane table, so it is tenant-keyed for this gate's purposes even
#: though it has no `workspace_id` column.
TENANT_ROOT = "workspaces"

#: The ratified posture on owner-bypass (`02` §7-DDL, verbatim): "ENABLE without
#: FORCE, deliberately: svc_migration owns the tables and owner-bypass is what
#: lets the runner transform without blanket migration policies".
#:
#: So FORCE is NOT required — requiring it would fail every table built to spec.
#: What IS gated is CONSISTENCY: every tenant-keyed table agrees. A mixed estate
#: is the dangerous state, because it reads as uniform to anyone spot-checking
#: one table, and the plan's rationale only holds if the posture is universal.
#: Flip this to True if the posture is ever re-ratified; the gate follows.
FORCE_REQUIRED = False

#: Tables that legitimately carry no tenant key. Kept as an explicit, empty
#: allowlist rather than omitted: `02` §7-DDL Class 3 (user-plane) and Class 4
#: (machinery counters, admission dedup) have no workspace column BY DESIGN,
#: and they are recognised here by the absence of the key, not by being named.
#: An entry belongs here only for a table that HAS a tenant key and is
#: nonetheless exempt — none exists today, and adding one should require
#: explaining why in review.
TENANT_KEY_EXEMPT: set[str] = set()

#: Statement kinds `expected_tenancy` may skip because they provably cannot move
#: any of the four facts a signature entry carries. Enumerated from the real
#: advertised stream rather than guessed — these are exactly the kinds the 257
#: statements contain that are not a table, an RLS enablement, or a policy.
#:
#: This is the ALLOWLIST half of that function's refusal: anything not matched
#: above and not named here raises, so a statement kind entering the plan is a
#: review event. Note `ALTER TABLE` is deliberately absent — today every one of
#: them is an `ENABLE ROW LEVEL SECURITY` that the function handles, and any
#: other form (`DROP COLUMN workspace_id`, `RENAME TO`) is precisely the
#: state-reducing case that must not pass silently.
_TENANCY_IRRELEVANT: tuple[str, ...] = (
    "CREATE INDEX ",
    "CREATE UNIQUE INDEX ",
    "CREATE TRIGGER ",
    "CREATE CONSTRAINT TRIGGER ",
    "CREATE FUNCTION ",
    "ALTER FUNCTION ",
    "CREATE SCHEMA ",
    "GRANT ",
    "REVOKE ",
    "INSERT INTO ",
    # 062 adds two kinds, both provably inert on the four facts: a COMMENT
    # changes no state at all, and DROP FUNCTION touches no table, policy or
    # RLS bit (the reducing hazards the refusal names are all table-shaped).
    "COMMENT ON ",
    "DROP FUNCTION ",
    # 069 (#1165) drops a partial unique index. An index is not a column, a
    # policy or an RLS bit — the four facts have exactly one writer each in
    # `expected_tenancy` and none of them is an index — so DROP INDEX is inert
    # here exactly as CREATE INDEX above already is.
    "DROP INDEX ",
    # 076 (the float, plan 03) replaces a door's body in place. A function
    # definition touches no table, policy or RLS bit — inert on the four facts
    # exactly as CREATE FUNCTION above; the `OR REPLACE` form is a new prefix,
    # not a new kind.
    "CREATE OR REPLACE FUNCTION ",
    # 098 revokes the pending invitations of inviters removed before it. An
    # UPDATE of a user table writes rows, not a table, a policy or an RLS bit:
    # inert on the four facts exactly as INSERT INTO above. Allowlisted for that
    # one table, not as a bare "UPDATE ", which would also admit a catalog
    # write such as `UPDATE pg_class SET relrowsecurity = false`.
    "UPDATE workspace_invitations ",
)


def _tenancy_entry(
    *,
    tenant_keyed: bool,
    rls_enabled: bool = False,
    rls_forced: bool = False,
    policies: int = 0,
) -> dict:
    """The one spelling of a signature entry.

    Two producers emit this shape — `tenancy_signature` from a live catalog and
    `expected_tenancy` from plan text — and callers compare them with `==`. A
    field added to one and not the other would not fail as "tenancy is wrong";
    it would redden every comparison for a reason unrelated to tenancy. Routing
    both through here makes that drift impossible rather than something a test
    has to notice.
    """
    return {
        "tenant_keyed": tenant_keyed,
        "rls_enabled": rls_enabled,
        "rls_forced": rls_forced,
        "policies": policies,
    }


def tenancy_signature(dsn: str) -> dict:
    """Per public table: is it tenant-keyed, is RLS on, how many policies.

    Deliberately three plain facts rather than the policy bodies. The gate's
    question is "was this table born tenant-scoped", which the counts answer;
    asserting policy *text* here would duplicate the plan and break on every
    legitimate rewording.
    """
    sig: dict[str, dict] = {}
    conn = psycopg2.connect(dsn)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT c.relname,"
                "       c.relrowsecurity,"
                "       c.relforcerowsecurity,"
                "       EXISTS (SELECT 1 FROM information_schema.columns col"
                "               WHERE col.table_schema = 'public'"
                "                 AND col.table_name = c.relname"
                "                 AND col.column_name = %s)"
                "  FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace"
                " WHERE n.nspname = 'public' AND c.relkind = 'r'",
                (TENANT_KEY,),
            )
            for name, rls, forced, has_key in cur.fetchall():
                sig[name] = _tenancy_entry(
                    tenant_keyed=bool(has_key) or name == TENANT_ROOT,
                    rls_enabled=bool(rls),
                    rls_forced=bool(forced),
                )

            cur.execute(
                "SELECT tablename, count(*) FROM pg_policies"
                " WHERE schemaname = 'public' GROUP BY tablename"
            )
            for name, count in cur.fetchall():
                if name in sig:
                    sig[name]["policies"] = count
    finally:
        conn.close()
    return sig


#: The forms this gate's two text reads — the compound guard and the column
#: names of a `CREATE TABLE` — refuse rather than read through. Any comment:
#: normalized text carries none, so one means text that never came through
#: `normalize_statements`, and a line comment on one line has lost the line end
#: that bounds it. An escape string and a dollar-quoted body, since their ends
#: turn on escape and tag rules, and a read that trusted them would turn a lexer
#: defect in those rules into an admission — the one failure these reads exist
#: to prevent. Unclosed text of any kind refuses too.
_UNREAD = frozenset({ESCAPE_STRING, DOLLAR}) | COMMENTS


#: The column an `ADD COLUMN` adds, read as the `CREATE TABLE` read reads one:
#: `sql_lexer.name_of` over a whole word, so `WORKSPACE_ID` is the key and
#: `workspace_id$old` is not. `IF NOT EXISTS` is skipped and never taken for the
#: name — the lookahead stops a backtrack reading `IF` as the column when a
#: quoted name follows it. A quoted name matches nothing here and falls through
#: to the refusal.
_ADD_COLUMN = re.compile(
    r"ALTER TABLE (?:public\.)?(\w+) ADD COLUMN (?:IF NOT EXISTS )?"
    rf"(?!IF NOT EXISTS\b)({WORD.pattern})"
)


def _top_level_comma(stmt: str) -> bool:
    """A comma outside every parenthesis — the mark of a compound ALTER.

    Depth is counted over code only. A literal or a quoted identifier is text,
    because a paren or comma in it moves nothing, and `scripts/sql_lexer.py`
    says where each one ends, shared with the splitter and the normalizer
    (#983, #1406).

    Everything else fails toward True, the refusal: text this scan will not
    read through (`_UNREAD`, or unclosed) reads as compound rather than being
    guessed at, and so do parens that do not balance.
    """
    depth = 0
    for seg in segments(stmt):
        if seg.kind in _UNREAD or not seg.closed:
            return True
        if seg.kind != CODE:
            continue
        for ch in seg.text:
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth < 0:
                    return True
            elif ch == "," and depth == 0:
                return True
    return depth != 0


def _names_the_tenant_key(columns: str) -> bool:
    """Whether a `CREATE TABLE` column list names `TENANT_KEY` — as a name in
    code, never as text in a literal: a DEFAULT string that merely mentions it
    is not the column (#1406). Each name is read by `sql_lexer.name_of`, as
    PostgreSQL resolves it.

    Refuses what it cannot read a name through (`_UNREAD`, unclosed), and a
    name `name_of` cannot resolve (`U&"…"`). A literal this read could not bound
    would hide the columns after it, and a keyed table read as unkeyed is the
    silent direction: nothing then asks it for RLS.
    """
    names = []  # None: a stretch of text no name can be read from
    for seg in segments(columns):
        if seg.kind in _UNREAD or not seg.closed:
            names.append(None)
        elif seg.kind == CODE:
            names += [name_of(word) for word in WORD.findall(seg.text)]
        elif seg.kind == IDENTIFIER:
            names.append(name_of(seg.text))
    if None in names:
        raise AssertionError(
            "this derivation cannot bound every name in the column list — an"
            " escape string, a dollar body, a comment, an unclosed quote or a U&"
            f" name — so it cannot say which columns the table has: {columns[:120]}"
        )
    return TENANT_KEY in names


def expected_tenancy(statements) -> dict:
    """The tenancy state a PREFIX of the advertised stream implies, in the same
    dict shape `tenancy_signature` reads off a live catalog.

    The two producers sit together deliberately: one derives from plan TEXT,
    the other observes a live CATALOG, and the whole value of comparing them is
    that they are different objects. A test that built both from the same source
    would be a tautology; this pair can disagree, and the disagreement is the
    finding — a statement the plan declares that the migration did not actually
    install shows up as a diff on one table rather than as a silent pass.

    WHY A PREFIX AND NOT A COMPLETE LINEAGE. Under #806 Fork 1 ruling (a) every
    F.2 increment is a contiguous segment of the stream, so the target lineage
    is always a positional prefix of it (that is what `f2_prefix_report` gates).
    The stream creates all 23 of `02`'s tables before the first ENABLE ROW LEVEL
    SECURITY, so "no tenant-keyed tables have landed yet" stops being true at
    F.2.2 and stays false until F.2.7 — a five-increment window in which a check
    written that way must either go red on schedule or be switched off. "The
    tenancy state matches what a prefix of this length implies" is true at EVERY
    increment instead, including the empty one, and it tightens by itself: at
    F.2.7 the implied state grows 23 RLS-enabled tables and 53 policies with no
    edit here. That is the alternative to bounding the check to a complete
    lineage, which buys green by going quiet for exactly the stretch the gate
    exists to cover.

    Note this is deliberately NOT `tenancy_violations` over a prefix. The
    invariant "every tenant-keyed table carries a policy" is FALSE mid-stream by
    the plan's own order, so asserting it early would be asserting the plan is
    wrong. What holds mid-stream is equality with the plan's own implication.

    Calibrated, not assumed: over the full 257-statement stream this derives 26
    tables and 19 tenant-keyed — the same counts a live replay of that stream
    observed (`test_advertised_ddl_replay`), with the other 7 carrying no
    workspace key by design (`02` §7-DDL Class 3/4).

    Takes normalized statements (`advertised_ddl.normalize_statements`) rather
    than raw SQL, so the caller owns the parse and this stays a pure function of
    a statement list.

    WHY IT LIVES HERE rather than in `advertised_ddl` beside `f2_prefix_report`,
    which is the other defensible home: it is bound to the tenancy VOCABULARY
    (`TENANT_KEY`, `TENANT_ROOT`, and `TENANT_KEY_EXEMPT` via the predicates
    below) and to the signature SHAPE, which `_tenancy_entry` now owns and both
    producers must build through. Splitting the two producers across modules
    would put the shape's definition one import away from half its users.

    An earlier version of this docstring justified the placement by claiming it
    kept psycopg2 out of `advertised_ddl`. That was FALSE and is recorded here so
    it is not re-derived: `scripts/` budgets stdlib **+ psycopg2** (see
    `schema_parity`), and measured, `advertised_ddl.normalize_statements` already
    loads psycopg2 transitively through `migration_runner` — which every caller
    of this function runs first. The dependency argument decides nothing; the
    cohesion one does.
    """
    sig: dict[str, dict] = {}
    for stmt in statements:
        # ASSUMES a space between the table name and its opening paren, which
        # holds for every `CREATE TABLE` the plan prints today. A future
        # `CREATE TABLE foo(` would not match, fall through to the refusal
        # below, and fail LOUDLY — but pointing at the wrong layer: it reads as
        # a plan/migration mismatch when it is a blind spot in this line.
        m = re.match(r"CREATE TABLE (?:IF NOT EXISTS )?(?:public\.)?(\w+) \((.*)", stmt)
        if m:
            name, body = m.group(1), m.group(2)
            sig[name] = _tenancy_entry(
                tenant_keyed=_names_the_tenant_key(body) or name == TENANT_ROOT
            )
            continue

        # Two different questions, and conflating them is what made an earlier
        # version raise on a policy for a table outside the prefix. "Did I
        # understand this statement KIND" decides whether to refuse; "does it
        # apply to state I hold" decides whether to record anything. A policy
        # naming a table this prefix has not created is understood perfectly
        # well — it simply has nothing to attach to, and inventing a phantom
        # entry for it would diverge from the catalog.
        # Bounded at the far end like ADD COLUMN and the constraint edits below
        # (#1412): the match anchors only the start, so `ENABLE ROW LEVEL
        # SECURITY, DROP COLUMN workspace_id` read as an enable. A top-level
        # comma is a second action and falls through to the refusal.
        m = re.match(r"ALTER TABLE (?:public\.)?(\w+) ENABLE ROW LEVEL SECURITY", stmt)
        if m and not _top_level_comma(stmt):
            if m.group(1) in sig:
                sig[m.group(1)]["rls_enabled"] = True
            continue

        m = re.match(r"CREATE POLICY \S+ ON (?:public\.)?(\w+)", stmt)
        if m:
            if m.group(1) in sig:
                sig[m.group(1)]["policies"] += 1
            continue

        # DROP POLICY is HANDLED, not allowlisted, because it moves a fact: it
        # takes a policy off its table. Only the plain form is read, one
        # unquoted name and no IF EXISTS, matched to the end of the statement:
        # a replay that got past it dropped exactly one policy, so the count
        # falls by one. `IF EXISTS` may drop nothing, which a count cannot
        # tell, so it falls through to the refusal below with every other
        # spelling. (094 is the first member.)
        m = re.match(
            r"DROP POLICY \w+ ON (?:public\.)?(\w+)(?: RESTRICT| CASCADE)?$", stmt
        )
        if m:
            if m.group(1) in sig:
                sig[m.group(1)]["policies"] -= 1
            continue

        # ADD COLUMN is HANDLED, not allowlisted, because one spelling of it
        # moves a fact: adding the tenant key itself flips tenant_keyed. Any
        # other column provably moves none of the four facts. A blanket
        # "ALTER TABLE " prefix would also admit DROP COLUMN workspace_id —
        # the exact reducing case the refusal below exists for — so the match
        # is on the ADD COLUMN form specifically (062 is the first member).
        #
        # The match is also bounded at the FAR end (#978 review): re.match
        # anchors only the start, so a compound statement — ADD COLUMN foo,
        # DROP COLUMN workspace_id — matches as a prefix and would ride the
        # continue past the refusal. A single ADD COLUMN never carries a
        # comma OUTSIDE parentheses and quotes (numeric(10,2) legitimately
        # carries one inside), so a top-level comma means a second action
        # and falls through to the loud refusal below — whatever the second
        # action is, including the fact-MOVING ones a verb denylist would
        # have to keep chasing (ADD COLUMN workspace_id, FORCE ROW LEVEL
        # SECURITY).
        m = _ADD_COLUMN.match(stmt)
        if m and not _top_level_comma(stmt):
            if name_of(m.group(2)) == TENANT_KEY and m.group(1) in sig:
                sig[m.group(1)]["tenant_keyed"] = True
            continue

        # CONSTRAINT edits are HANDLED for the same reason ADD COLUMN is, and
        # allowlisted for none of them: a blanket "ALTER TABLE " prefix is the
        # very thing the note on _TENANCY_IRRELEVANT refuses, because it would
        # admit DROP COLUMN workspace_id alongside these.
        #
        # Neither form can move any of the four facts, and the argument is
        # exhaustive rather than plausible — each fact has exactly one writer
        # in this function: tenant_keyed only from ADD COLUMN workspace_id,
        # rls_enabled only from ENABLE ROW LEVEL SECURITY, policies only from
        # CREATE POLICY, rls_forced never set here at all. A CHECK constraint
        # adds and removes no column, no policy and no RLS bit, so it cannot
        # reach any of them.
        #
        # ADD is matched only in its CHECK form. FOREIGN KEY / UNIQUE / PRIMARY
        # KEY are equally inert on these four today, and are deliberately left
        # to the refusal anyway: the narrower claim is the one that stays true,
        # and a new constraint SHAPE entering the plan should be a review event
        # rather than something this branch already waved through. (065 is the
        # first member — #1061's job kind.)
        #
        # Bounded at the far end by the same _top_level_comma guard ADD COLUMN
        # uses, so a compound `DROP CONSTRAINT x, DROP COLUMN workspace_id`
        # falls through to the refusal instead of riding this continue.
        if not _top_level_comma(stmt) and (
            re.match(r"ALTER TABLE (?:public\.)?\w+ DROP CONSTRAINT \w+", stmt)
            or re.match(r"ALTER TABLE (?:public\.)?\w+ ADD CONSTRAINT \w+ CHECK", stmt)
        ):
            continue

        # ALTER INDEX is HANDLED, not allowlisted, because one spelling of it
        # moves a table: for backward compatibility PostgreSQL also runs
        # `ALTER INDEX … RENAME TO` on a TABLE and renames it (measured on 15:
        # the relation renamed is relkind 'r'; every other ALTER INDEX form
        # refuses a non-index). A table's name is the key every fact here is
        # recorded under, so a rename through the index form is the reducing
        # case `ALTER TABLE … RENAME TO` is. An ALTER INDEX naming no table
        # this prefix created acts on an index, which is none of the four
        # facts; one naming such a table falls through to the refusal below.
        # 089 (#1413) is the first member: it renames a partial unique index
        # to the name of the key it replaces.
        m = re.match(r"ALTER INDEX (?:IF EXISTS )?(?:public\.)?(\w+) ", stmt)
        if m and m.group(1).lower() not in {name.lower() for name in sig}:
            continue  # an unquoted name folds to lower case, as the server folds it

        # ALLOWLIST, not a denylist, and the direction is the whole point.
        #
        # What must never happen is a statement that REDUCES tenancy state
        # being silently skipped — the derivation would then claim a policy or
        # a table is present that the replay has since dropped, and the lane
        # comparison would fail against the catalog for a reason nobody can
        # find. A `DROP POLICY` the branch above does not read is only the
        # obvious member; `DROP TABLE`, `ALTER TABLE … DROP COLUMN
        # workspace_id`, `ALTER TABLE … RENAME TO` and `DROP SCHEMA … CASCADE`
        # all do it too.
        #
        # An earlier version named two of those and let everything else fall
        # through to a silent ignore — which is a claim about today's corpus
        # ("the stream builds, it never tears down") wearing the shape of an
        # enforced invariant. The bounded side is what the derivation HANDLES;
        # the ignorable side is anything anyone can write in a plan document.
        # So the bounded side is enumerated and the rest refuses, the same
        # treatment `TENANT_KEY_EXEMPT` gets above and the same philosophy as
        # `advertised_ddl`'s manifest ratchet: a new statement kind entering
        # the plan is a review event, not a guess.
        if not stmt.startswith(_TENANCY_IRRELEVANT):
            raise AssertionError(
                f"statement kind this derivation does not classify, so it "
                f"cannot promise the derived state is complete. Add it to "
                f"_TENANCY_IRRELEVANT if it provably cannot move "
                f"tenant_keyed/rls_enabled/rls_forced/policies, or handle it "
                f"above if it can: {stmt[:120]}"
            )

    return sig


def tenant_keyed_tables(sig: dict) -> set[str]:
    """The tables this gate demands tenancy of — one definition, because the
    callers that DISCLOSE coverage must scope it the same way the gate scopes
    enforcement. Held apart from `tenancy_violations` so a disclosure can ask
    "is there anything here to check yet" without re-deriving the answer and
    drifting from it: a private copy that forgot `TENANT_KEY_EXEMPT` would go
    red naming a table the gate is deliberately silent about.
    """
    return {
        t for t, e in sig.items() if e["tenant_keyed"] and t not in TENANT_KEY_EXEMPT
    }


def tenancy_violations(sig: dict) -> list[str]:
    """Tables that are tenant-keyed but not born tenant-scoped.

    Two separate failures, reported separately because they are different
    mistakes with the same symptom in a schema dump:

    * **RLS off** — the policies may exist and are simply not enforced.
    * **RLS on, no policy** — enforced, denying everything, which reads as
      "secured" to anyone checking `relrowsecurity` alone and fails closed in a
      way that looks like a bug elsewhere.
    """
    out: list[str] = []
    keyed = tenant_keyed_tables(sig)

    # THIRD INVARIANT — owner-bypass posture (rajan, #750 review). Policies do
    # not apply to a table's OWNER unless FORCE is set, so ENABLE + policies +
    # no FORCE leaves the table readable wholesale by the owning role. The plan
    # accepts that deliberately and says why; what it cannot survive is a MIXED
    # estate, where some tables are owner-bypassable and some are not and
    # nothing says which. Checked as agreement with FORCE_REQUIRED so the gate
    # states the posture rather than inferring it from whatever landed first.
    forced = {t for t in keyed if sig[t].get("rls_forced")}
    deviating = (keyed - forced) if FORCE_REQUIRED else forced
    for table in sorted(deviating):
        want = (
            "FORCE is required"
            if FORCE_REQUIRED
            else "the posture is ENABLE without FORCE"
        )
        have = "not forced" if FORCE_REQUIRED else "FORCE is set"
        out.append(
            f"{table}: owner-bypass posture deviates — {have}, but {want} "
            f"(`02` §7-DDL). A mixed estate is the failure: policies do not "
            f"apply to the table OWNER unless FORCE is set, so half the tables "
            f"being owner-readable while the other half are not is invisible to "
            f"anyone spot-checking one of them"
        )

    for table in sorted(sig):
        entry = sig[table]
        if not entry["tenant_keyed"] or table in TENANT_KEY_EXEMPT:
            continue
        if not entry["rls_enabled"]:
            out.append(
                f"{table}: tenant-keyed but RLS is not enabled — `02` §7 prints "
                f"ENABLE ROW LEVEL SECURITY in the same replay as the table"
            )
        elif not entry["policies"]:
            out.append(
                f"{table}: RLS enabled but carries no policy — every row is "
                f"denied to every login, which is not what 'born tenant-scoped' "
                f"means"
            )
    return out
