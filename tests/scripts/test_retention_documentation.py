"""Keep the documented retention boundary aligned with the database door."""

from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[2]
POLICY = ROOT / "documentation/operations/retention-policy.md"
FUNCTIONS = ROOT / "src/models/target/ddl/04_functions.sql"

EXPECTED_OFF = {
    "audit_events",
    "jobs",
    "provider_operations",
    "channel_outbox",
    "daily_post_counts",
}


def test_retention_policy_matches_the_database_allowlist():
    policy = POLICY.read_text()
    function_sql = FUNCTIONS.read_text()

    function = re.search(
        r"CREATE FUNCTION fn_retention_batch\b(?P<body>.*?)\nEND \$\$;",
        function_sql,
        re.DOTALL,
    )
    assert function, "fn_retention_batch definition was not found"
    accepted = set(re.findall(r"p_class\s*=\s*'([^']+)'", function["body"]))

    active_rows = re.findall(r"^\| `([^`]+)` \| periodic retention on \|", policy, re.M)
    off_rows = re.findall(r"^\| `([^`]+)` \| periodic retention off \|", policy, re.M)

    assert active_rows == sorted(accepted)
    assert set(off_rows) == EXPECTED_OFF
    assert len(off_rows) == len(EXPECTED_OFF)
    assert "scheduler.execute_retention_sweep" in policy
