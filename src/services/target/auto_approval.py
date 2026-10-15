"""The serve step's decision: a story approved automatically, or left to a
person.

This is the one module that decides. :func:`decide` is pure, and two callers
share it: the serve step (`prompts.sweep_due_prompts`' advance leg, through
:func:`approve_if_due`) and a waiting card's block note
(`prompts.rerender_prompt`). So a card never says something the sweep did not
do. The order, where the first match wins:

1. **Not eligible → none.** A cancel is pending, API posting is off, there is
   no working login and dry run is off, or the item has a link to add.
2. **The item is flagged → ``item``.**
3. **The position is ``auto`` → ``auto``.** The position is the folder's own
   when it has one, else the workspace's.
4. **The position is ``returning`` and the item has posted through the API
   before → ``returning``.** Never a post made by hand, never a dry run.
5. **Otherwise → none.**

The ledger admits the approval: `tg_intent_person_or_rule` (`approval position constraints`) takes
`awaiting_approval → approved` from the system only when the same UPDATE
stamps the rule, and `intent_ledger.transition` is that UPDATE's one writer.
This module writes no card: the story's card renders settled when it is sent
(`prompts.rerender_prompt`).
"""

from __future__ import annotations

import logging
from typing import NamedTuple, Optional

from src.services.target import ig_credentials, intent_ledger, jobs, readers, vocabulary

logger = logging.getLogger(__name__)


class Facts(NamedTuple):
    """What the decision reads about one story, in one statement."""

    provider_account_ref: str
    cancel_requested: bool
    api_publishing_enabled: bool
    dry_run: bool
    has_login: bool
    has_link: bool
    flagged: bool
    position: str
    posted_via_api: bool


class Decision(NamedTuple):
    """The rule that approves the story, or None. When step 1 stopped a rule,
    *blocked_by* says why (``api_off``, ``no_login`` or ``link``) and
    *would_be* is that rule, which a waiting card names. A story nothing would
    approve has no block to report, and a pending cancel is no note's
    business."""

    rule: Optional[str]
    blocked_by: Optional[str] = None
    would_be: Optional[str] = None


#: The decision's own read, never `prompts._CARD_SELECT`: that select is pinned
#: verbatim to `fn_prompts_due`'s body. Every table is bound on the workspace.
#: `has_login` is `approve`'s own precondition, the one spelling
#: (`ig_credentials.usable_login_sql`), and `posted_via_api` is a probe of
#: `ix_intents_api_posted` (`approval position constraints`), whose predicate it repeats.
_FACTS = (
    "SELECT i.provider_account_ref, i.cancel_requested,"
    "       w.api_publishing_enabled, w.dry_run_mode,"
    "       COALESCE(s.approval_mode, w.approval_mode) AS position,"
    "       m.auto_approve, m.link_url IS NOT NULL AS has_link,"
    "       "
    + ig_credentials.usable_login_sql(workspace=":ws", account="i.ig_account_id")
    + " AS has_login,"
    "       EXISTS (SELECT 1 FROM post_intents p"
    "                WHERE p.workspace_id = :ws AND p.media_item_id = i.media_item_id"
    "                  AND p.state = 'posted' AND p.published_via = 'api')"
    "         AS posted_via_api"
    "  FROM post_intents i"
    "  JOIN workspaces w ON w.id = :ws"
    "  JOIN media_items m ON m.workspace_id = :ws AND m.id = i.media_item_id"
    "  LEFT JOIN media_sources s ON s.workspace_id = :ws AND s.id = m.source_id"
    " WHERE i.workspace_id = :ws AND i.id = :id"
)


async def facts(session, *, workspace_id: str, intent_id: str) -> Optional[Facts]:
    """The story's facts, or None when this session cannot see it."""
    row = await readers.row(
        session,
        _FACTS,
        ws=str(workspace_id),
        id=str(intent_id),
        ig_provider=vocabulary.PROVIDER_IG_LOGIN,
    )
    if row is None:
        return None
    return Facts(
        provider_account_ref=str(row["provider_account_ref"]),
        cancel_requested=bool(row["cancel_requested"]),
        api_publishing_enabled=bool(row["api_publishing_enabled"]),
        dry_run=bool(row["dry_run_mode"]),
        has_login=bool(row["has_login"]),
        has_link=bool(row["has_link"]),
        flagged=bool(row["auto_approve"]),
        position=str(row["position"]),
        posted_via_api=bool(row["posted_via_api"]),
    )


def _rule(f: Facts) -> Optional[str]:
    """Steps 2–4."""
    if f.flagged:
        return "item"
    if f.position == "auto":
        return "auto"
    if f.position == "returning" and f.posted_via_api:
        return "returning"
    return None


def decide(f: Facts) -> Decision:
    """The rule that approves the story, in the module's order."""
    rule = _rule(f)
    if rule is None or f.cancel_requested:
        return Decision(None)
    if not f.api_publishing_enabled:
        return Decision(None, "api_off", rule)
    if not f.has_login and not f.dry_run:
        return Decision(None, "no_login", rule)
    if f.has_link:
        return Decision(None, "link", rule)
    return Decision(rule)


async def approve_if_due(
    session, *, workspace_id: str, intent_id: str
) -> Optional[str]:
    """Approve one waiting story when a rule says so: the approval edge as the
    system, stamped with the rule, and the same `publish_pipeline` job a
    person's `approve` mints. Returns the rule, or None when the story waits
    for a person. In the caller's transaction, and under its claim of the
    workspace (the `system` actor the trigger requires)."""
    from src.services.target import source_policy

    verdict = await source_policy.intent_suitability(
        session, workspace_id=workspace_id, intent_id=intent_id
    )
    if verdict is None or not verdict["suitable"]:
        return None
    found = await facts(session, workspace_id=workspace_id, intent_id=intent_id)
    rule = decide(found).rule if found is not None else None
    if rule is None:
        return None
    await intent_ledger.transition(
        session, str(intent_id), "approved", approval_rule=rule
    )
    await jobs.mint_publish_job(
        session,
        workspace_id=str(workspace_id),
        intent_id=str(intent_id),
        provider_account_ref=found.provider_account_ref,
        dry_run=found.dry_run,
    )
    logger.info("story %s approved automatically (%s)", intent_id, rule)
    return rule
