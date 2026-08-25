"""Two readers over `text()` SQL, so a read model is one ``return``.

The tier's reads are raw SQL against the replayed schema; what varies is the
statement, not the five lines of ``execute`` / ``mappings`` / ``dict`` around
it. *executor* is anything with ``.execute`` (an `AsyncSession` or an
`AsyncConnection`), as everywhere else in `src/services/target/`.
"""

from __future__ import annotations

from typing import Any, Optional

from sqlalchemy import text


async def rows(executor, sql: str, **params) -> list[dict[str, Any]]:
    return [dict(r) for r in (await executor.execute(text(sql), params)).mappings()]


async def row(executor, sql: str, **params) -> Optional[dict[str, Any]]:
    found = (await executor.execute(text(sql), params)).mappings().first()
    return dict(found) if found else None
