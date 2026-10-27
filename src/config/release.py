"""The commit a deployment runs, as the API and the worker both name it.

One reading of one variable, so `/health/scheduling` can set the API's commit
beside the worker's and a difference between them is a real difference in
what each service runs.
"""

from __future__ import annotations

from typing import Mapping, Optional

#: Railway's variable for the commit a deployment was built from.
COMMIT_VAR = "RAILWAY_GIT_COMMIT_SHA"


def short_commit(env: Mapping[str, str]) -> Optional[str]:
    """The deployment's commit as its first seven characters, or None when the
    variable is unset or blank."""
    return (env.get(COMMIT_VAR) or "").strip()[:7] or None
