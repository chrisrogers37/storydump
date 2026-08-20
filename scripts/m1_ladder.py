"""The owner-derivation ladder (`04` M.1 / `02` §9), as ONE expression.

    the `user_chat_memberships` row with instance_role='owner' (earliest
    joined_at if several); else earliest 'admin'; else earliest active
    membership; else the chat goes to the window's quarantine list.

**Why this is a module and not four branches inside a transform file.** The
ladder is consumed twice — by the M.1a preflight, which must say *before* the
window whether every chat resolves, and by the M.1c transform, which must pick
the same user the preflight promised. Two copies of a four-rung precedence rule
is how the preflight blesses a chat the transform then quarantines.

## The rung-1/2 activity ambiguity is NOT resolved here

`04` L80 qualifies only the third rung as "earliest **active** membership".
Read literally, rungs 1 and 2 see inactive rows too. But the very next bullet
says ``is_active=false`` rows are dropped — and the derived owner MUST land as a
``workspace_members`` row (``ct_workspaces_owner_at_insert``), so selecting an
owner from a row the same transform drops is incoherent.

Both readings are defensible and production cannot separate them (every
membership row there is active, and no chat has an owner or admin row at all).
So this module **computes both and reports disagreement** rather than picking:

- ``LADDER_SQL_PERMISSIVE`` — rungs 1/2 ignore ``is_active`` (the literal read)
- ``LADDER_SQL_STRICT``     — every rung requires ``is_active`` (the coherent read)

A ruling collapses these to one. Until then, a corpus on which they disagree is
a corpus where the ruling changes who owns a workspace, and the preflight says
so. Guessing would produce a preflight that is confidently wrong on exactly the
rows a human needed to see.

## Determinism

``ORDER BY rung, joined_at, user_id`` — the trailing ``user_id`` is not
decoration. ``joined_at`` has no uniqueness constraint, so two rows can tie, and
a two-key sort would leave the winner to the plan. A transform that picks a
different owner on a re-run is not re-runnable, and the M.2 rehearsal exists to
be repeated.
"""

from __future__ import annotations

#: Rung numbers are stable identifiers, printed in reports and asserted in
#: tests. Rung 4 is the ABSENCE of a rung, never a value in the CASE.
RUNG_OWNER, RUNG_ADMIN, RUNG_ACTIVE, RUNG_QUARANTINE = 1, 2, 3, 4

RUNG_NAMES = {
    RUNG_OWNER: "rung1-owner-row",
    RUNG_ADMIN: "rung2-earliest-admin",
    RUNG_ACTIVE: "rung3-earliest-active",
    RUNG_QUARANTINE: "rung4-QUARANTINE",
}


def _ladder(strict: bool) -> str:
    """The ladder. `strict` decides whether rungs 1 and 2 require is_active."""
    gate = " AND m.is_active" if strict else ""
    return f"""
WITH rungs AS (
    SELECT m.chat_settings_id,
           m.user_id,
           m.joined_at,
           CASE
             WHEN m.instance_role = 'owner'{gate} THEN {RUNG_OWNER}
             WHEN m.instance_role = 'admin'{gate} THEN {RUNG_ADMIN}
             WHEN m.is_active                     THEN {RUNG_ACTIVE}
           END AS rung
      FROM user_chat_memberships m
)
SELECT cs.id                                  AS chat_settings_id,
       pick.user_id                           AS owner_user_id,
       COALESCE(pick.rung, {RUNG_QUARANTINE}) AS rung
  FROM chat_settings cs
  LEFT JOIN LATERAL (
      SELECT r.user_id, r.rung
        FROM rungs r
       WHERE r.chat_settings_id = cs.id
         AND r.rung IS NOT NULL
       ORDER BY r.rung, r.joined_at, r.user_id
       LIMIT 1
  ) pick ON TRUE
"""


LADDER_SQL_PERMISSIVE = _ladder(strict=False)
LADDER_SQL_STRICT = _ladder(strict=True)

#: Chats where the two readings pick a different owner, or disagree on whether
#: the chat resolves at all. Empty here means the ruling is not yet load-bearing
#: ON THIS DATA — which is a fact about the data, never a substitute for it.
LADDER_DISAGREEMENT_SQL = f"""
WITH permissive AS ({LADDER_SQL_PERMISSIVE}), strict AS ({LADDER_SQL_STRICT})
SELECT p.chat_settings_id, p.owner_user_id, p.rung, s.owner_user_id, s.rung
  FROM permissive p
  JOIN strict s USING (chat_settings_id)
 WHERE p.owner_user_id IS DISTINCT FROM s.owner_user_id
    OR p.rung IS DISTINCT FROM s.rung
"""
